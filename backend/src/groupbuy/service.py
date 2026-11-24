"""团购票登记、整单剔除与复盘失效的领域逻辑。

团购票按订单号登记，可以分次补录。每补录一次就为对应座位写入正常的票房观测，
所以所有座位都照常进入票房统计和班结对账；但只要一张订单的座位横跨 control 与
treatment 两组，整单就退出实验样本。晚到补录让原先入样的订单变成跨组时，引用过
它的复盘失效（旧签字保留），必须重算后再审。
"""
import hashlib
import json
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import select

from ..models import AuditLog, ExperimentUnit, RevenueReview, TicketObservation
from .models import (GroupOrder, GroupOrderEntry, GroupOrderSeat, ReviewGroupSnapshot,
                     ReviewInvalidation)

INVALIDATED = 'invalidated'
GROUP_SOURCE = 'group-buy'


def _audit(db, actor, action, entity_type, entity_id, detail=''):
    db.add(AuditLog(actor_id=actor.id if actor else None, action=action, entity_type=entity_type,
                    entity_id=str(entity_id), detail=detail))


def collect_orders(db, plan_id):
    """按订单汇总当前补录，返回订单视图（含跨组判定）。"""
    orders = db.scalars(select(GroupOrder).where(GroupOrder.plan_id == plan_id)
                        .order_by(GroupOrder.id)).all()
    if not orders:
        return []
    entries = db.scalars(select(GroupOrderEntry).where(
        GroupOrderEntry.order_id.in_([order.id for order in orders]))
        .order_by(GroupOrderEntry.id)).all()
    units = {}
    if entries:
        units = {unit.id: unit for unit in db.scalars(select(ExperimentUnit).where(
            ExperimentUnit.id.in_({entry.unit_id for entry in entries}))).all()}
    by_order = defaultdict(list)
    for entry in entries:
        by_order[entry.order_id].append(entry)
    result = []
    for order in orders:
        rows = []
        groups = defaultdict(int)
        for entry in by_order[order.id]:
            unit = units[entry.unit_id]
            groups[unit.group_name] += entry.seats
            rows.append({
                'id': entry.id, 'unit_id': entry.unit_id, 'hall_name': unit.hall_name,
                'session_code': unit.session_code, 'seat_zone': unit.seat_zone,
                'group_name': unit.group_name, 'seats': entry.seats, 'note': entry.note,
                'recorded_at': entry.recorded_at.isoformat() if entry.recorded_at else None,
            })
        excluded = len(groups) > 1
        result.append({
            'id': order.id, 'order_no': order.order_no, 'note': order.note,
            'seat_count': sum(entry.seats for entry in by_order[order.id]),
            'groups': dict(groups), 'group_names': sorted(groups), 'excluded': excluded,
            'exclusion_reason': 'spans_groups' if excluded else None,
            'entries': rows,
            'created_at': order.created_at.isoformat() if order.created_at else None,
        })
    return result


def _snapshot_payload(orders):
    return [{
        'order_no': order['order_no'], 'seats': order['seat_count'], 'groups': order['groups'],
        'units': [{'unit_id': entry['unit_id'], 'hall_name': entry['hall_name'],
                   'session_code': entry['session_code'], 'seat_zone': entry['seat_zone'],
                   'group_name': entry['group_name'], 'seats': entry['seats']}
                  for entry in order['entries']],
    } for order in orders]


def _signature(included):
    canonical = sorted(
        [order['order_no'], sorted([[entry['unit_id'], entry['seats']] for entry in order['entries']])]
        for order in included)
    payload = json.dumps({'v': 1, 'orders': canonical}, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def current_snapshot(db, plan_id):
    """当前团购样本：入样 / 剔除明细与指纹。"""
    orders = collect_orders(db, plan_id)
    included = [order for order in orders if not order['excluded']]
    excluded = [order for order in orders if order['excluded']]
    return {
        'included': included,
        'excluded': excluded,
        'signature': _signature(included),
        'included_json': json.dumps(_snapshot_payload(included), ensure_ascii=False),
        'excluded_json': json.dumps(_snapshot_payload(excluded), ensure_ascii=False),
        'included_seats': sum(order['seat_count'] for order in included),
        'excluded_seats': sum(order['seat_count'] for order in excluded),
    }


def excluded_order_ids(db, plan_id):
    return [order['id'] for order in collect_orders(db, plan_id) if order['excluded']]


def excluded_observation_ids(db, plan_id):
    """跨组订单下的所有票房观测，均退出实验样本。"""
    order_ids = excluded_order_ids(db, plan_id)
    if not order_ids:
        return set()
    entry_ids = db.scalars(select(GroupOrderEntry.id).where(
        GroupOrderEntry.order_id.in_(order_ids))).all()
    if not entry_ids:
        return set()
    return set(db.scalars(select(GroupOrderSeat.observation_id).where(
        GroupOrderSeat.entry_id.in_(entry_ids))).all())


def invalidate_review(db, review, reason, actor=None, excluded=None):
    """保留旧签字字段，只更新状态并写入失效留档。"""
    if review.analysis_status == INVALIDATED:
        return False
    review.analysis_status = INVALIDATED
    db.add(ReviewInvalidation(review_id=review.id, reason=reason,
                              excluded_json=json.dumps(excluded or [], ensure_ascii=False),
                              invalidated_by=actor.id if actor else None))
    if review.plan is not None:
        others = db.scalars(select(RevenueReview).where(
            RevenueReview.plan_id == review.plan_id, RevenueReview.id != review.id,
            RevenueReview.analysis_status == 'approved')).all()
        if not others and review.plan.status == 'approved':
            review.plan.status = 'live'
    _audit(db, actor, 'review_invalidated', 'revenue_review', review.id, reason)
    return True


def invalidate_reviews_using(db, plan_id, order_no, actor=None):
    """晚到补录使订单变为跨组时，让引用过该订单的复盘失效。"""
    snapshots = db.scalars(select(ReviewGroupSnapshot).join(
        RevenueReview, ReviewGroupSnapshot.review_id == RevenueReview.id).where(
        RevenueReview.plan_id == plan_id)).all()
    if not snapshots:
        return []
    review_ids = [snapshot.review_id for snapshot in snapshots]
    reviews = {review.id: review for review in db.scalars(select(RevenueReview).where(
        RevenueReview.id.in_(review_ids))).all()}
    excluded_orders = {order['order_no']: order for order in current_snapshot(db, plan_id)['excluded']}
    invalidated = []
    for snapshot in snapshots:
        try:
            included = json.loads(snapshot.included_json or '[]')
        except ValueError:
            continue
        if not any(order.get('order_no') == order_no for order in included):
            continue
        review = reviews.get(snapshot.review_id)
        if not review or review.analysis_status not in {'ready', 'approved'}:
            continue
        reason = f'团购订单 {order_no} 晚到补录后横跨两组，整单退出实验样本，复盘需重算后再审'
        # 失效留档只记录本次触发剔除的订单，复盘页面据此展示剔除订单号和座位数。
        payload = _snapshot_payload([excluded_orders[order_no]] if order_no in excluded_orders else [])
        if invalidate_review(db, review, reason, actor, payload):
            invalidated.append(review)
    return invalidated


def record_review_snapshot(db, review, plan_id):
    snapshot = current_snapshot(db, plan_id)
    db.add(ReviewGroupSnapshot(review_id=review.id, signature=snapshot['signature'],
                               included_json=snapshot['included_json'],
                               excluded_json=snapshot['excluded_json']))
    return snapshot


def review_sample_changed(db, plan_id, review):
    """复盘生成后团购样本是否变化。没有指纹的历史复盘不在此处拦截。"""
    snapshot = db.scalar(select(ReviewGroupSnapshot).where(
        ReviewGroupSnapshot.review_id == review.id))
    if snapshot is None:
        return False, None
    current = current_snapshot(db, plan_id)
    return snapshot.signature != current['signature'], current


def review_group_detail(db, review):
    """复盘页面展示的团购样本：入样数、剔除订单号与座位数。

    失效复盘还要展示使其失效的整单剔除明细，即使当时该订单仍在样本中。
    """
    snapshot = db.scalar(select(ReviewGroupSnapshot).where(
        ReviewGroupSnapshot.review_id == review.id))
    invalidation = db.scalar(select(ReviewInvalidation).where(
        ReviewInvalidation.review_id == review.id))
    included, excluded = [], []
    if snapshot:
        try:
            included = json.loads(snapshot.included_json or '[]')
            excluded = json.loads(snapshot.excluded_json or '[]')
        except ValueError:
            included, excluded = [], []
    invalidation_excluded = []
    if invalidation:
        try:
            invalidation_excluded = json.loads(invalidation.excluded_json or '[]')
        except ValueError:
            invalidation_excluded = []
    if invalidation_excluded:
        known = {order.get('order_no') for order in excluded}
        excluded = excluded + [order for order in invalidation_excluded
                               if order.get('order_no') not in known]
        dropped = {order.get('order_no') for order in invalidation_excluded}
        included = [order for order in included if order.get('order_no') not in dropped]
    return {
        'included_group_orders': included,
        'excluded_group_orders': excluded,
        'included_group_seats': sum(order.get('seats', 0) for order in included),
        'excluded_group_seats': sum(order.get('seats', 0) for order in excluded),
        'signature': snapshot.signature if snapshot else None,
        'invalidated_at': invalidation.invalidated_at.isoformat() if invalidation else None,
        'invalidated_reason': invalidation.reason if invalidation else None,
    }


def _seats_recorded(db, order_id):
    """订单已补录的座位总数，用作下一批座位编号的起点。"""
    return sum(seats for seats in db.scalars(select(GroupOrderEntry.seats).where(
        GroupOrderEntry.order_id == order_id)).all())


def register_entry(db, plan, payload, actor):
    """按订单号登记（或补录）团购票，返回订单视图、新观测和失效复盘。"""
    unit = db.get(ExperimentUnit, payload.unit_id)
    if not unit or unit.plan_id != plan.id:
        raise HTTPException(404, '实验单元不存在')
    if unit.status != 'started':
        raise HTTPException(409, '只能向已开始的场次登记团购票')
    order = db.scalar(select(GroupOrder).where(GroupOrder.plan_id == plan.id,
                                               GroupOrder.order_no == payload.order_no))
    existed = order is not None
    if order is None:
        order = GroupOrder(plan_id=plan.id, order_no=payload.order_no, note=payload.note,
                           created_by=actor.id)
        db.add(order)
        db.flush()
    before = next((row for row in collect_orders(db, plan.id)
                   if row['order_no'] == payload.order_no), None)
    start = _seats_recorded(db, order.id)
    entry = GroupOrderEntry(order_id=order.id, unit_id=unit.id, seats=payload.seats,
                            note=payload.note, recorded_by=actor.id)
    db.add(entry)
    db.flush()
    observations = []
    for offset in range(payload.seats):
        sequence = start + offset + 1
        observation = TicketObservation(
            unit_id=unit.id, ticket_code=f"GROUP-{order.id}-{sequence:04d}",
            seat_label=f"G{sequence:02d}", sold=True, price_paid=0, source=GROUP_SOURCE)
        db.add(observation)
        observations.append(observation)
    db.flush()
    for observation in observations:
        db.add(GroupOrderSeat(entry_id=entry.id, observation_id=observation.id))
    db.flush()
    after = next(row for row in collect_orders(db, plan.id)
                 if row['order_no'] == payload.order_no)
    invalidated = []
    became_excluded = after['excluded'] and not (before and before['excluded'])
    if became_excluded:
        invalidated = invalidate_reviews_using(db, plan.id, payload.order_no, actor)
        _audit(db, actor, 'group_order_excluded', 'group_order', order.id,
               f"{payload.order_no} · seats={after['seat_count']} · groups={after['group_names']}")
    _audit(db, actor, 'group_order_entry_recorded', 'group_order', order.id,
           f"{payload.order_no} · unit={unit.id} · seats={payload.seats}"
           f"{' · created' if not existed else ' · supplemented'}")
    return order, after, observations, invalidated
