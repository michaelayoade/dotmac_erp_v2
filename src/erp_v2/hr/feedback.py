"""HR appraisal feedback, appeals and outcome actions — e2 slice 4.

Appeals are the guarded consequence path after completion: one open
appeal per appraisal, resolution may adjust the final rating, and
every adjustment writes an outcome action so the original score math
stays auditable.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from erp_v2.hr.appraisals import Appraisal, AppraisalStatus
from erp_v2.hr.models import _tenant_fk


class AppealStatus(str, enum.Enum):
    FILED = "filed"
    IN_MEDIATION = "in_mediation"
    IN_COMMITTEE = "in_committee"
    RESOLVED = "resolved"
    REJECTED = "rejected"


_OPEN_APPEAL = (AppealStatus.FILED.value, AppealStatus.IN_MEDIATION.value, AppealStatus.IN_COMMITTEE.value)


class AppraisalFeedback(Base, TimestampMixin):
    __tablename__ = "hr_appraisal_feedback"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_feedback_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "appraisal_id"],
            ["hr_appraisals.tenant_id", "hr_appraisals.id"],
            ondelete="CASCADE",
            name="fk_hr_feedback_tenant_appraisal",
        ),
        Index("ix_hr_feedback_tenant_appraisal", "tenant_id", "appraisal_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    appraisal_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    feedback_from_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    feedback_type: Mapped[str] = mapped_column(String(30), nullable=False)
    overall_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    areas_for_improvement: Mapped[str | None] = mapped_column(Text, nullable=True)
    general_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submitted_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppraisalAppeal(Base, TimestampMixin):
    __tablename__ = "hr_appraisal_appeals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_appeals_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "appraisal_id"],
            ["hr_appraisals.tenant_id", "hr_appraisals.id"],
            ondelete="CASCADE",
            name="fk_hr_appeals_tenant_appraisal",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_appeals_tenant_employee",
        ),
        CheckConstraint(
            "status IN ('filed','in_mediation','in_committee','resolved','rejected')",
            name="ck_hr_appeals_status",
        ),
        Index("ix_hr_appeals_tenant_appraisal", "tenant_id", "appraisal_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    appraisal_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="filed")
    filed_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    mediator_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    adjusted_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    resolution_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppraisalOutcomeAction(Base, TimestampMixin):
    __tablename__ = "hr_appraisal_outcome_actions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_outcome_actions_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "appraisal_id"],
            ["hr_appraisals.tenant_id", "hr_appraisals.id"],
            ondelete="CASCADE",
            name="fk_hr_outcome_actions_tenant_appraisal",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    appraisal_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    actioned_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    actioned_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="recorded")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


FEEDBACK_TABLES = (
    "hr_appraisal_feedback",
    "hr_appraisal_appeals",
    "hr_appraisal_outcome_actions",
)


class FeedbackError(RuntimeError):
    """A feedback/appeal rule refusal."""


def _appraisal(session: Session, tenant_id: UUID, appraisal_id: UUID) -> Appraisal:
    appraisal = session.scalar(select(Appraisal).where(Appraisal.tenant_id == tenant_id, Appraisal.id == appraisal_id))
    if appraisal is None:
        raise FeedbackError("appraisal not found")
    return appraisal


def submit_feedback(
    session: Session,
    *,
    tenant_id: UUID,
    appraisal_id: UUID,
    feedback_type: str,
    feedback_from_id: UUID | None = None,
    overall_rating: Decimal | None = None,
    strengths: str | None = None,
    areas_for_improvement: str | None = None,
    general_comments: str | None = None,
    is_anonymous: bool = False,
    rating_scale_max: Decimal = Decimal(5),
) -> AppraisalFeedback:
    appraisal = _appraisal(session, tenant_id, appraisal_id)
    if appraisal.status == AppraisalStatus.CANCELLED.value:
        raise FeedbackError("cannot add feedback to a cancelled appraisal")
    if overall_rating is not None and not (Decimal(0) <= overall_rating <= rating_scale_max):
        raise FeedbackError(f"overall_rating outside 0..{rating_scale_max}")
    if is_anonymous:
        feedback_from_id = None
    row = AppraisalFeedback(
        tenant_id=tenant_id,
        appraisal_id=appraisal_id,
        feedback_from_id=feedback_from_id,
        feedback_type=feedback_type,
        overall_rating=overall_rating,
        strengths=strengths,
        areas_for_improvement=areas_for_improvement,
        general_comments=general_comments,
        is_anonymous=is_anonymous,
        submitted_on=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def file_appeal(
    session: Session,
    *,
    tenant_id: UUID,
    appraisal_id: UUID,
    filed_date: date,
    reason: str,
    requested_outcome: str | None = None,
) -> AppraisalAppeal:
    """One open appeal per completed appraisal."""

    appraisal = _appraisal(session, tenant_id, appraisal_id)
    if appraisal.status != AppraisalStatus.COMPLETED.value:
        raise FeedbackError("only a completed appraisal can be appealed")
    open_appeal = session.scalar(
        select(AppraisalAppeal).where(
            AppraisalAppeal.tenant_id == tenant_id,
            AppraisalAppeal.appraisal_id == appraisal_id,
            AppraisalAppeal.status.in_(_OPEN_APPEAL),
        )
    )
    if open_appeal is not None:
        raise FeedbackError("an open appeal already exists for this appraisal")
    appeal = AppraisalAppeal(
        tenant_id=tenant_id,
        appraisal_id=appraisal_id,
        employee_id=appraisal.employee_id,
        filed_date=filed_date,
        reason=reason,
        requested_outcome=requested_outcome,
    )
    session.add(appeal)
    session.flush()
    return appeal


def resolve_appeal(
    session: Session,
    *,
    tenant_id: UUID,
    appeal_id: UUID,
    upheld: bool,
    resolution_date: date,
    adjusted_rating: Decimal | None = None,
    resolution_notes: str | None = None,
    actioned_by_id: UUID | None = None,
) -> AppraisalAppeal:
    """Resolve an open appeal; an upheld appeal adjusts the final rating
    and records the adjustment as an outcome action."""

    appeal = session.scalar(
        select(AppraisalAppeal).where(AppraisalAppeal.tenant_id == tenant_id, AppraisalAppeal.id == appeal_id)
    )
    if appeal is None:
        raise FeedbackError("appeal not found")
    if appeal.status not in _OPEN_APPEAL:
        raise FeedbackError(f"appeal already closed in status {appeal.status!r}")
    appeal.resolution_date = resolution_date
    appeal.resolution_notes = resolution_notes
    if not upheld:
        appeal.status = AppealStatus.REJECTED.value
        session.flush()
        return appeal
    if adjusted_rating is None:
        raise FeedbackError("an upheld appeal needs an adjusted_rating")
    appraisal = _appraisal(session, tenant_id, appeal.appraisal_id)
    previous = appraisal.final_rating
    appraisal.final_rating = adjusted_rating
    appeal.adjusted_rating = adjusted_rating
    appeal.status = AppealStatus.RESOLVED.value
    session.add(
        AppraisalOutcomeAction(
            tenant_id=tenant_id,
            appraisal_id=appraisal.id,
            action_type="rating_adjusted_on_appeal",
            description=f"final rating {previous} -> {adjusted_rating} on upheld appeal",
            actioned_by_id=actioned_by_id,
            actioned_date=resolution_date,
        )
    )
    session.flush()
    return appeal


__all__ = [
    "FEEDBACK_TABLES",
    "AppealStatus",
    "AppraisalAppeal",
    "AppraisalFeedback",
    "AppraisalOutcomeAction",
    "FeedbackError",
    "file_appeal",
    "resolve_appeal",
    "submit_feedback",
]
