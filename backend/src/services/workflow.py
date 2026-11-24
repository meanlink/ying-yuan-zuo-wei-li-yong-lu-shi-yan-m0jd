from datetime import datetime, timezone
import json
from fastapi import HTTPException
from sqlalchemy.orm import Session
from ..groupbuy import service as groupbuy
from ..models import AuditLog, ExperimentUnit, RevenueReview, ScreeningPlan, TicketObservation, User
from .analytics import balance_ratio, bootstrap_difference, stratified_effect

def audit(db: Session, user: User | None, action, entity_type, entity_id, detail=""):
    db.add(AuditLog(actor_id=user.id if user else None, action=action, entity_type=entity_type, entity_id=str(entity_id), detail=detail))

def start_experiment(db: Session, plan: ScreeningPlan, user: User):
    if plan.status not in {"draft", "configured"}:
        raise HTTPException(409, "排片实验已开始，不能重复启动")
    units = db.query(ExperimentUnit).filter_by(plan_id=plan.id).all()
    if not units:
        raise HTTPException(422, "请先配置实验单元")
    groups = {u.group_name for u in units}
    if groups != {"control", "treatment"}:
        raise HTTPException(422, "实验单元必须同时包含 control 和 treatment")
    now = datetime.now(timezone.utc)
    for unit in units:
        unit.status = "started"
        unit.started_at = now
    plan.status = "live"
    audit(db, user, "experiment_started", "screening_plan", plan.id, f"units={len(units)}")

def _sample_rows(db: Session, plan: ScreeningPlan):
    """把票房观测转换成分析行；整单跨组的团购座位退出实验样本。"""
    units = db.query(ExperimentUnit).filter_by(plan_id=plan.id).all()
    unit_map = {u.id: u for u in units}
    excluded = groupbuy.excluded_observation_ids(db, plan.id)
    observations = db.query(TicketObservation).filter(
        TicketObservation.unit_id.in_([u.id for u in units])).all()
    return [{"sold": o.sold, "group": unit_map[o.unit_id].group_name,
             "stratum": f"{unit_map[o.unit_id].hall_name}/{unit_map[o.unit_id].session_code}"}
            for o in observations if o.id not in excluded]


def run_analysis(db: Session, plan: ScreeningPlan, user: User, force_stratified=False):
    rows = _sample_rows(db, plan)
    units = db.query(ExperimentUnit).filter_by(plan_id=plan.id).all()
    observation_count = db.query(TicketObservation).filter(
        TicketObservation.unit_id.in_([u.id for u in units])).count()
    control = [r for r in rows if r["group"] == "control"]
    treatment = [r for r in rows if r["group"] == "treatment"]
    result = bootstrap_difference(control, treatment)
    ratio = balance_ratio(rows)
    use_stratified = force_stratified or ratio < 0.78
    strat = stratified_effect(rows)
    status = "ready" if (ratio >= 0.78 or force_stratified) else "rerun_required"
    snapshot = groupbuy.current_snapshot(db, plan.id)
    review = RevenueReview(plan_id=plan.id, analysis_status=status, sample_size=len(rows), control_rate=sum(x["sold"] for x in control) / len(control), treatment_rate=sum(x["sold"] for x in treatment) / len(treatment), absolute_diff=result["difference"], ci_low=result["ci_low"], ci_high=result["ci_high"], stratified_effect=strat["effect"], balance_ratio=ratio, is_stratified=use_stratified, result_json=json.dumps({"bootstrap": result, "strata": strat["strata"], "observation_count": observation_count, "group_orders": {"included_seats": snapshot["included_seats"], "excluded_seats": snapshot["excluded_seats"]}}, ensure_ascii=False))
    db.add(review)
    db.flush()
    groupbuy.record_review_snapshot(db, review, plan.id)
    plan.status = "review" if status == "ready" else "live"
    audit(db, user, "analysis_run", "revenue_review", plan.id, f"status={status};stratified={use_stratified}")
    return review

def approve_review(db: Session, review: RevenueReview, user: User, notes: str):
    if review.analysis_status != "ready":
        raise HTTPException(409, "跨厅样本不平衡，需要先重跑分层分析")
    changed, _ = groupbuy.review_sample_changed(db, review.plan_id, review)
    if changed:
        raise HTTPException(409, "团购样本在复盘后发生变化，请重跑分析后再审批")
    review.analysis_status = "approved"
    review.reviewer_id = user.id
    review.notes = notes
    review.approved_at = datetime.now(timezone.utc)
    review.plan.status = "approved"
    audit(db, user, "review_approved", "revenue_review", review.id, notes)
