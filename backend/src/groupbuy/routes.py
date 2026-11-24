from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import current_user, require_roles
from ..database import get_db
from ..models import User
from ..services.operations import plan_by_id
from . import service
from .schemas import SubmitGroupEntry

router = APIRouter(prefix='/plans/{plan_id}/group-orders', tags=['团购票登记'])

boxoffice = require_roles('boxoffice', 'admin')


def order_json(order):
    return {
        'id': order['id'], 'order_no': order['order_no'], 'note': order['note'],
        'seat_count': order['seat_count'], 'groups': order['groups'],
        'group_names': order['group_names'], 'excluded': order['excluded'],
        'exclusion_reason': order['exclusion_reason'], 'created_at': order['created_at'],
        'entries': order['entries'],
    }


@router.get('')
def list_orders(plan_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    plan_by_id(db, plan_id)
    return {'items': [order_json(order) for order in service.collect_orders(db, plan_id)]}


@router.post('', status_code=201)
def record_entry(plan_id: int, payload: SubmitGroupEntry, db: Session = Depends(get_db),
                 user: User = Depends(boxoffice)):
    plan = plan_by_id(db, plan_id, lock=True)
    if plan.status not in ('live', 'review', 'approved'):
        raise HTTPException(409, '实验开始前不能登记团购票')
    order, after, observations, invalidated = service.register_entry(db, plan, payload, user)
    result = order_json(after)
    result['observation_ids'] = [observation.id for observation in observations]
    result['invalidated_reviews'] = [
        {'id': review.id, 'analysis_status': review.analysis_status,
         'had_approval': review.approved_at is not None}
        for review in invalidated
    ]
    db.commit()
    return result


@router.get('/excluded')
def excluded(plan_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    plan_by_id(db, plan_id)
    orders = [order for order in service.collect_orders(db, plan_id) if order['excluded']]
    return {
        'items': [order_json(order) for order in orders],
        'seat_total': sum(order['seat_count'] for order in orders),
    }


@router.get('/summary')
def summary(plan_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    plan_by_id(db, plan_id)
    orders = service.collect_orders(db, plan_id)
    excluded = [order for order in orders if order['excluded']]
    return {
        'order_count': len(orders),
        'seat_total': sum(order['seat_count'] for order in orders),
        'excluded_count': len(excluded),
        'excluded_seats': sum(order['seat_count'] for order in excluded),
    }
