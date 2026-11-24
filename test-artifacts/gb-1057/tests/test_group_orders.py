"""团购票登记 / 整单剔除 / 复盘失效的端到端测试。

运行：python -m pytest test-artifacts/gb-1057/tests
使用临时 SQLite 数据库，不需要 Docker 或 PostgreSQL。
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest

_TMP = tempfile.mkdtemp(prefix='gb1057-')
os.environ.setdefault('DATABASE_URL', f'sqlite+pysqlite:///{_TMP}/test.db')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
os.environ.setdefault('SEED_DEMO', 'false')
BACKEND = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'backend')
sys.path.insert(0, os.path.abspath(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src import database  # noqa: E402

_engine = create_engine(os.environ['DATABASE_URL'], connect_args={'check_same_thread': False})
database.engine = _engine
database.SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

from src.database import Base  # noqa: E402
from src import operations_models  # noqa: F401,E402
from src.groupbuy import models as groupbuy_models  # noqa: F401,E402
from src.settlement import models as settlement_models  # noqa: F401,E402
from src.groupbuy import service as groupbuy  # noqa: E402
from src.models import ExperimentUnit, ScreeningPlan, TicketObservation, User  # noqa: E402
from src.security import hash_password  # noqa: E402


PLAN_STATUS_LIVE = 'live'


def _reset():
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)


@pytest.fixture()
def env():
    _reset()
    db = database.SessionLocal()
    users = {}
    for email, role in [('planner', 'planner'), ('analyst', 'analyst'),
                        ('revenue', 'revenue'), ('boxoffice', 'boxoffice')]:
        user = User(email=f'{email}@x', name=role, role=role, password_hash=hash_password('x'))
        db.add(user)
        users[role] = user
    db.flush()
    plan = ScreeningPlan(natural_key='GB-TEST', film_title='F', version_name='v',
                         show_date=date(2026, 11, 21), show_time='19:30', hall_name='H',
                         seat_capacity=200, base_price=50, status=PLAN_STATUS_LIVE)
    db.add(plan)
    db.flush()
    control = ExperimentUnit(plan_id=plan.id, session_code='S1', hall_name='H1', seat_zone='A',
                             group_name='control', seat_count=40, status='started',
                             started_at=datetime.now(timezone.utc))
    treatment = ExperimentUnit(plan_id=plan.id, session_code='S1', hall_name='H1', seat_zone='B',
                               group_name='treatment', seat_count=40, status='started',
                               started_at=datetime.now(timezone.utc))
    db.add_all([control, treatment])
    db.flush()
    for i in range(8):
        db.add(TicketObservation(unit_id=control.id, ticket_code=f'C-{i}', seat_label=f'A{i}',
                                 sold=i % 2 == 0, price_paid=50))
        db.add(TicketObservation(unit_id=treatment.id, ticket_code=f'T-{i}', seat_label=f'B{i}',
                                 sold=i % 2 == 0, price_paid=50))
    db.commit()
    yield {'db': db, 'users': users, 'plan': plan, 'control': control, 'treatment': treatment}
    db.close()


def _register(env, order_no, unit, seats, user='boxoffice'):
    obj, after, observations, invalidated = groupbuy.register_entry(
        env['db'], env['plan'],
        __import__('src.groupbuy.schemas', fromlist=['SubmitGroupEntry']).SubmitGroupEntry(
            order_no=order_no, unit_id=unit.id, seats=seats, note='n'),
        env['users'][user])
    env['db'].commit()
    return after, observations, invalidated


def test_supplement_accumulates_on_same_order(env):
    after, observations, invalidated = _register(env, 'GB1', env['control'], 2)
    assert after['seat_count'] == 2 and not after['excluded'] and not invalidated
    assert len(observations) == 2
    after, observations, _ = _register(env, 'GB1', env['control'], 3)
    assert after['seat_count'] == 5 and len(after['entries']) == 2
    assert len(observations) == 3


def test_group_seats_enter_ticketing_ledger(env):
    _register(env, 'GB1', env['control'], 4)
    count = env['db'].query(TicketObservation).filter_by(source=groupbuy.GROUP_SOURCE).count()
    assert count == 4
    total = env['db'].query(TicketObservation).filter(
        TicketObservation.unit_id.in_([env['control'].id, env['treatment'].id])).count()
    assert total == 20  # 16 normal + 4 group seats, all in the ledger


def test_cross_group_order_excluded_whole(env):
    _register(env, 'GB1', env['control'], 3)
    after, _, _ = _register(env, 'GB1', env['treatment'], 2)
    assert after['excluded'] and after['group_names'] == ['control', 'treatment']
    assert after['seat_count'] == 5
    rows, _ = groupbuy.current_snapshot(env['db'], env['plan'].id), None
    assert rows['excluded_seats'] == 5 and rows['included_seats'] == 0


def test_late_supplement_invalidates_used_review(env):
    _register(env, 'GB1', env['control'], 3)
    from src.services.workflow import approve_review, run_analysis
    review = run_analysis(env['db'], env['plan'], env['users']['analyst'], force_stratified=True)
    env['db'].commit()
    approve_review(env['db'], review, env['users']['revenue'], 'ok')
    env['db'].commit()
    approved_at = review.approved_at
    reviewer = review.reviewer_id

    _, _, invalidated = _register(env, 'GB1', env['treatment'], 1)
    env['db'].refresh(review)
    assert [row.id for row in invalidated] == [review.id]
    assert review.analysis_status == 'invalidated'
    # 旧签字留档：审批人与时间不因失效而清空
    assert review.approved_at == approved_at and review.reviewer_id == reviewer


def test_invalidated_review_shows_excluded_orders(env):
    _register(env, 'GB1', env['control'], 3)
    from src.services.workflow import approve_review, run_analysis
    review = run_analysis(env['db'], env['plan'], env['users']['analyst'], force_stratified=True)
    env['db'].commit()
    approve_review(env['db'], review, env['users']['revenue'], 'ok')
    env['db'].commit()
    _register(env, 'GB1', env['treatment'], 1)
    detail = groupbuy.review_group_detail(env['db'], review)
    assert detail['excluded_group_seats'] == 4
    assert [order['order_no'] for order in detail['excluded_group_orders']] == ['GB1']


def test_excluded_seats_leave_sample_on_rerun(env):
    _register(env, 'GB1', env['control'], 3)
    from src.services.workflow import run_analysis
    first = run_analysis(env['db'], env['plan'], env['users']['analyst'], force_stratified=True)
    env['db'].commit()
    assert first.sample_size == 19  # 16 normal + 3 group
    _register(env, 'GB1', env['treatment'], 1)
    second = run_analysis(env['db'], env['plan'], env['users']['analyst'], force_stratified=True)
    env['db'].commit()
    assert second.sample_size == 16  # cross-group order wholly dropped


def test_invalidation_unblocks_plan_and_blocks_new_pilots(env):
    _register(env, 'GB1', env['control'], 3)
    from src.services.workflow import approve_review, run_analysis
    review = run_analysis(env['db'], env['plan'], env['users']['analyst'], force_stratified=True)
    env['db'].commit()
    approve_review(env['db'], review, env['users']['revenue'], 'ok')
    env['db'].commit()
    assert env['plan'].status == 'approved'
    _register(env, 'GB1', env['treatment'], 1)
    env['db'].refresh(env['plan'])
    assert env['plan'].status == 'live'


def test_approve_rejects_review_whose_group_sample_changed(env):
    _register(env, 'GB1', env['control'], 2)
    from src.services.workflow import run_analysis
    review = run_analysis(env['db'], env['plan'], env['users']['analyst'], force_stratified=True)
    env['db'].commit()
    _register(env, 'GB1', env['control'], 2)  # same group, sample changed
    with pytest.raises(Exception) as error:
        from src.services.workflow import approve_review
        approve_review(env['db'], review, env['users']['revenue'], 'ok')
    assert getattr(error.value, 'status_code', None) == 409


def test_approve_rejects_invalidated_review(env):
    _register(env, 'GB1', env['control'], 1)
    from src.services.workflow import approve_review, run_analysis
    review = run_analysis(env['db'], env['plan'], env['users']['analyst'], force_stratified=True)
    env['db'].commit()
    approve_review(env['db'], review, env['users']['revenue'], 'ok')
    env['db'].commit()
    _register(env, 'GB1', env['treatment'], 1)
    with pytest.raises(Exception) as error:
        approve_review(env['db'], review, env['users']['revenue'], 'again')
    assert getattr(error.value, 'status_code', None) == 409


def test_review_snapshot_is_immutable_after_supplement(env):
    _register(env, 'GB1', env['control'], 2)
    from src.services.workflow import run_analysis
    review = run_analysis(env['db'], env['plan'], env['users']['analyst'], force_stratified=True)
    env['db'].commit()
    _register(env, 'GB1', env['control'], 1)
    changed, _ = groupbuy.review_sample_changed(env['db'], env['plan'].id, review)
    assert changed
