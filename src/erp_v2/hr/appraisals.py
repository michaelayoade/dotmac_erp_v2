"""HR appraisals — ERP-unique slice 3 (legacy: appraisal* + kra).

Core flow only: cycle + template (with weighted KRAs) -> appraisal
-> KRA scores -> self/manager/calibrated ratings -> completion.
Feedback, appeals and outcome actions import as pairings now and
port as the next slice; their tables are declared in e2 pairings.
Weighted score math is pure Decimal over score rows; the status
machine refuses out-of-order transitions.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from erp_v2.hr.models import _tenant_fk


class AppraisalStatus(str, enum.Enum):
    DRAFT = "draft"
    SELF_SUBMITTED = "self_submitted"
    MANAGER_REVIEWED = "manager_reviewed"
    CALIBRATED = "calibrated"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


_ORDER = (
    AppraisalStatus.DRAFT,
    AppraisalStatus.SELF_SUBMITTED,
    AppraisalStatus.MANAGER_REVIEWED,
    AppraisalStatus.CALIBRATED,
    AppraisalStatus.COMPLETED,
)


class AppraisalCycle(Base, TimestampMixin):
    __tablename__ = "hr_appraisal_cycles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_cycles_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_appraisal_cycles_tenant_code"),
        CheckConstraint("status IN ('draft','open','closed')", name="ck_hr_appraisal_cycles_status"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    review_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    self_assessment_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    manager_review_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")


class Kra(Base, TimestampMixin):
    __tablename__ = "hr_kras"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_kras_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_kras_tenant_code"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    measurement_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class AppraisalTemplate(Base, TimestampMixin):
    __tablename__ = "hr_appraisal_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_templates_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_appraisal_templates_tenant_code"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating_scale_max: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal(5), server_default="5"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class AppraisalTemplateKra(Base, TimestampMixin):
    __tablename__ = "hr_appraisal_template_kras"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_template_kras_tenant_id_id"),
        UniqueConstraint("tenant_id", "template_id", "kra_id", name="uq_hr_template_kra"),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["hr_appraisal_templates.tenant_id", "hr_appraisal_templates.id"],
            ondelete="CASCADE",
            name="fk_hr_template_kras_tenant_template",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "kra_id"],
            ["hr_kras.tenant_id", "hr_kras.id"],
            ondelete="RESTRICT",
            name="fk_hr_template_kras_tenant_kra",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    template_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    kra_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    weightage: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Appraisal(Base, TimestampMixin):
    __tablename__ = "hr_appraisals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_appraisals_tenant_id_id"),
        UniqueConstraint("tenant_id", "cycle_id", "employee_id", name="uq_hr_appraisals_cycle_employee"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_appraisals_tenant_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["hr_appraisal_cycles.tenant_id", "hr_appraisal_cycles.id"],
            ondelete="RESTRICT",
            name="fk_hr_appraisals_tenant_cycle",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["hr_appraisal_templates.tenant_id", "hr_appraisal_templates.id"],
            ondelete="RESTRICT",
            name="fk_hr_appraisals_tenant_template",
        ),
        CheckConstraint(
            "status IN ('draft','self_submitted','manager_reviewed','calibrated','completed','cancelled')",
            name="ck_hr_appraisals_status",
        ),
        Index("ix_hr_appraisals_tenant_employee", "tenant_id", "employee_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    cycle_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    template_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    manager_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    self_overall_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    self_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    manager_overall_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    manager_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    calibrated_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    calibration_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    final_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    completed_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppraisalKraScore(Base, TimestampMixin):
    __tablename__ = "hr_appraisal_kra_scores"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_kra_scores_tenant_id_id"),
        UniqueConstraint("tenant_id", "appraisal_id", "kra_id", name="uq_hr_appraisal_kra"),
        ForeignKeyConstraint(
            ["tenant_id", "appraisal_id"],
            ["hr_appraisals.tenant_id", "hr_appraisals.id"],
            ondelete="CASCADE",
            name="fk_hr_kra_scores_tenant_appraisal",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "kra_id"],
            ["hr_kras.tenant_id", "hr_kras.id"],
            ondelete="RESTRICT",
            name="fk_hr_kra_scores_tenant_kra",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    appraisal_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    kra_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    weightage: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    self_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    self_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    manager_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    manager_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    weighted_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)


APPRAISAL_TABLES = (
    "hr_appraisal_cycles",
    "hr_kras",
    "hr_appraisal_templates",
    "hr_appraisal_template_kras",
    "hr_appraisals",
    "hr_appraisal_kra_scores",
)


class AppraisalError(RuntimeError):
    """An appraisal rule refusal."""


def weighted_final(scores: list[AppraisalKraScore]) -> Decimal:
    """Sum(final_rating * weightage) / 100; unrated scores refuse."""

    total_weight = sum((s.weightage for s in scores), Decimal(0))
    if total_weight != Decimal(100):
        raise AppraisalError(f"KRA weightage must total 100, got {total_weight}")
    total = Decimal(0)
    for score in scores:
        if score.final_rating is None:
            raise AppraisalError("every KRA needs a final rating before completion")
        score.weighted_score = (score.final_rating * score.weightage).quantize(Decimal("0.01"))
        total += score.weighted_score
    return (total / Decimal(100)).quantize(Decimal("0.01"))


def _get(session: Session, tenant_id: UUID, appraisal_id: UUID) -> Appraisal:
    appraisal = session.scalar(select(Appraisal).where(Appraisal.tenant_id == tenant_id, Appraisal.id == appraisal_id))
    if appraisal is None:
        raise AppraisalError("appraisal not found")
    return appraisal


def advance(
    session: Session,
    *,
    tenant_id: UUID,
    appraisal_id: UUID,
    to_status: AppraisalStatus,
) -> Appraisal:
    """Move exactly one step forward; completing computes the final score."""

    appraisal = _get(session, tenant_id, appraisal_id)
    if appraisal.status == AppraisalStatus.CANCELLED.value:
        raise AppraisalError("cancelled appraisals cannot advance")
    current = _ORDER.index(AppraisalStatus(appraisal.status))
    if _ORDER.index(to_status) != current + 1:
        raise AppraisalError(
            f"cannot move {appraisal.status!r} -> {to_status.value!r}; next is {_ORDER[current + 1].value!r}"
            if current + 1 < len(_ORDER)
            else "appraisal is already completed"
        )
    if to_status is AppraisalStatus.COMPLETED:
        scores = list(
            session.scalars(
                select(AppraisalKraScore).where(
                    AppraisalKraScore.tenant_id == tenant_id,
                    AppraisalKraScore.appraisal_id == appraisal_id,
                )
            )
        )
        if not scores:
            raise AppraisalError("no KRA scores to complete")
        appraisal.final_score = weighted_final(scores)
        appraisal.final_rating = appraisal.calibrated_rating or appraisal.final_score
        appraisal.completed_on = datetime.now(timezone.utc)
    appraisal.status = to_status.value
    session.flush()
    return appraisal


__all__ = [
    "APPRAISAL_TABLES",
    "Appraisal",
    "AppraisalCycle",
    "AppraisalError",
    "AppraisalKraScore",
    "AppraisalStatus",
    "AppraisalTemplate",
    "AppraisalTemplateKra",
    "Kra",
    "advance",
    "weighted_final",
]
