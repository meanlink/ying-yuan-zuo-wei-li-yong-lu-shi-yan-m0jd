from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import now_utc


class GroupOrder(Base):
    """一张团购订单。补录只追加记录，订单号在同一排片内唯一。"""

    __tablename__ = 'group_orders'
    __table_args__ = (UniqueConstraint('plan_id', 'order_no', name='uq_group_order_no'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey('screening_plans.id'), index=True)
    order_no: Mapped[str] = mapped_column(String(60))
    note: Mapped[str] = mapped_column(Text, default='')
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class GroupOrderEntry(Base):
    """订单的一次补录，座位落在某个实验单元上。"""

    __tablename__ = 'group_order_entries'
    __table_args__ = (CheckConstraint('seats > 0'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('group_orders.id'), index=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey('experiment_units.id'), index=True)
    seats: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default='')
    recorded_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class GroupOrderSeat(Base):
    """补录座位与票务观测一一对应，团购座位照常进入票房台账。"""

    __tablename__ = 'group_order_seats'
    __table_args__ = (UniqueConstraint('observation_id', name='uq_group_seat_observation'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey('group_order_entries.id'), index=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey('ticket_observations.id'), index=True)


class ReviewGroupSnapshot(Base):
    """复盘生成时的团购样本指纹，用于发现晚到补录导致的样本变化。"""

    __tablename__ = 'review_group_snapshots'
    __table_args__ = (UniqueConstraint('review_id', name='uq_review_group_snapshot'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey('revenue_reviews.id'), index=True)
    signature: Mapped[str] = mapped_column(String(64))
    included_json: Mapped[str] = mapped_column(Text, default='[]')
    excluded_json: Mapped[str] = mapped_column(Text, default='[]')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ReviewInvalidation(Base):
    """失效复盘的留档：原签字字段保留在复盘行上，这里记录失效原因和时间。"""

    __tablename__ = 'review_invalidations'
    __table_args__ = (UniqueConstraint('review_id', name='uq_review_invalidation'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey('revenue_reviews.id'), index=True)
    reason: Mapped[str] = mapped_column(Text)
    excluded_json: Mapped[str] = mapped_column(Text, default='[]')
    invalidated_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    invalidated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
