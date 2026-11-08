"""HR lifecycle — ERP-unique slice 7: onboarding, separation, and
promotion/transfer employment changes (legacy: checklist_template*,
employee_onboarding*, employee_separation*, clearance_item,
employee_promotion*, employee_transfer*).

Onboarding materializes checklist activities from a template and
derives progress from activity completions — progress is never a
stored counter someone can drift. Separation completes only when
every clearance item is cleared. Promotion/transfer share one
header+detail shape (property changes), as legacy kept them.
"""

from __future__ import annotations

import enum
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from erp_v2.hr.models import _tenant_fk


class OnboardingStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SeparationStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ChecklistTemplate(Base, TimestampMixin):
    __tablename__ = "hr_checklist_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_checklist_templates_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_checklist_templates_tenant_code"),
        CheckConstraint("template_type IN ('onboarding','separation')", name="ck_hr_checklist_type"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_type: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class ChecklistTemplateItem(Base, TimestampMixin):
    __tablename__ = "hr_checklist_template_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_checklist_items_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["hr_checklist_templates.tenant_id", "hr_checklist_templates.id"],
            ondelete="CASCADE",
            name="fk_hr_checklist_items_tenant_template",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    template_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    default_assignee_role: Mapped[str | None] = mapped_column(String(60), nullable=True)
    days_from_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requires_document: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())


class EmployeeOnboarding(Base, TimestampMixin):
    __tablename__ = "hr_employee_onboardings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_onboardings_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_onboardings_tenant_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["hr_checklist_templates.tenant_id", "hr_checklist_templates.id"],
            ondelete="RESTRICT",
            name="fk_hr_onboardings_tenant_template",
        ),
        CheckConstraint("status IN ('in_progress','completed','cancelled')", name="ck_hr_onboardings_status"),
        Index("ix_hr_onboardings_tenant_employee", "tenant_id", "employee_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    template_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    date_of_joining: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
    buddy_employee_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    manager_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    expected_completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class OnboardingActivity(Base, TimestampMixin):
    __tablename__ = "hr_onboarding_activities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_onboarding_activities_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "onboarding_id"],
            ["hr_employee_onboardings.tenant_id", "hr_employee_onboardings.id"],
            ondelete="CASCADE",
            name="fk_hr_activities_tenant_onboarding",
        ),
        CheckConstraint("status IN ('pending','completed','skipped')", name="ck_hr_activities_status"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    onboarding_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    template_item_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    document_file_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    completion_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmployeeSeparation(Base, TimestampMixin):
    __tablename__ = "hr_employee_separations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_separations_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_separations_tenant_employee",
        ),
        CheckConstraint("status IN ('in_progress','completed','cancelled')", name="ck_hr_separations_status"),
        Index("ix_hr_separations_tenant_employee", "tenant_id", "employee_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    separation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    separation_date: Mapped[date] = mapped_column(Date, nullable=False)
    resignation_letter_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason_for_leaving: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_interview_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClearanceItem(Base, TimestampMixin):
    __tablename__ = "hr_clearance_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_clearance_items_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "separation_id"],
            ["hr_employee_separations.tenant_id", "hr_employee_separations.id"],
            ondelete="CASCADE",
            name="fk_hr_clearance_tenant_separation",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    separation_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_to_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    is_cleared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    cleared_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    cleared_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmploymentChange(Base, TimestampMixin):
    """Promotion/transfer header (legacy kept both in this shape)."""

    __tablename__ = "hr_employment_changes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_employment_changes_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_changes_tenant_employee",
        ),
        CheckConstraint("change_type IN ('promotion','transfer')", name="ck_hr_changes_type"),
        Index("ix_hr_changes_tenant_employee", "tenant_id", "employee_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)
    change_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmploymentChangeDetail(Base, TimestampMixin):
    __tablename__ = "hr_employment_change_details"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_change_details_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "change_id"],
            ["hr_employment_changes.tenant_id", "hr_employment_changes.id"],
            ondelete="CASCADE",
            name="fk_hr_change_details_tenant_change",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    change_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    property_name: Mapped[str] = mapped_column(String(80), nullable=False)
    current_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


LIFECYCLE_TABLES = (
    "hr_checklist_templates",
    "hr_checklist_template_items",
    "hr_employee_onboardings",
    "hr_onboarding_activities",
    "hr_employee_separations",
    "hr_clearance_items",
    "hr_employment_changes",
    "hr_employment_change_details",
)


class LifecycleError(RuntimeError):
    """A lifecycle rule refusal."""


def start_onboarding(
    session: Session,
    *,
    tenant_id: UUID,
    employee_id: UUID,
    template_id: UUID,
    date_of_joining: date,
    buddy_employee_id: UUID | None = None,
    manager_id: UUID | None = None,
) -> EmployeeOnboarding:
    """Create onboarding and materialize its template activities."""

    template = session.scalar(
        select(ChecklistTemplate).where(
            ChecklistTemplate.tenant_id == tenant_id,
            ChecklistTemplate.id == template_id,
            ChecklistTemplate.template_type == "onboarding",
            ChecklistTemplate.is_active.is_(True),
        )
    )
    if template is None:
        raise LifecycleError("active onboarding template not found")
    items = list(
        session.scalars(
            select(ChecklistTemplateItem)
            .where(
                ChecklistTemplateItem.tenant_id == tenant_id,
                ChecklistTemplateItem.template_id == template_id,
            )
            .order_by(ChecklistTemplateItem.sequence)
        )
    )
    onboarding = EmployeeOnboarding(
        tenant_id=tenant_id,
        employee_id=employee_id,
        template_id=template_id,
        date_of_joining=date_of_joining,
        buddy_employee_id=buddy_employee_id,
        manager_id=manager_id,
    )
    session.add(onboarding)
    session.flush()
    for item in items:
        session.add(
            OnboardingActivity(
                tenant_id=tenant_id,
                onboarding_id=onboarding.id,
                template_item_id=item.id,
                name=item.name,
                category=item.category,
                sequence=item.sequence,
                due_date=(
                    date_of_joining + timedelta(days=item.days_from_start) if item.days_from_start is not None else None
                ),
            )
        )
    session.flush()
    return onboarding


def onboarding_progress(session: Session, *, tenant_id: UUID, onboarding_id: UUID) -> Decimal:
    """Derived completion percentage; never a stored counter."""

    total = (
        session.scalar(
            select(func.count())
            .select_from(OnboardingActivity)
            .where(
                OnboardingActivity.tenant_id == tenant_id,
                OnboardingActivity.onboarding_id == onboarding_id,
            )
        )
        or 0
    )
    if total == 0:
        return Decimal(0)
    done = (
        session.scalar(
            select(func.count())
            .select_from(OnboardingActivity)
            .where(
                OnboardingActivity.tenant_id == tenant_id,
                OnboardingActivity.onboarding_id == onboarding_id,
                OnboardingActivity.status.in_(("completed", "skipped")),
            )
        )
        or 0
    )
    return (Decimal(done) * Decimal(100) / Decimal(total)).quantize(Decimal("0.01"))


def complete_activity(
    session: Session,
    *,
    tenant_id: UUID,
    activity_id: UUID,
    completed_on: date,
    completed_by_id: UUID | None = None,
    document_file_id: UUID | None = None,
) -> OnboardingActivity:
    activity = session.scalar(
        select(OnboardingActivity).where(
            OnboardingActivity.tenant_id == tenant_id,
            OnboardingActivity.id == activity_id,
        )
    )
    if activity is None:
        raise LifecycleError("onboarding activity not found")
    if activity.status != "pending":
        raise LifecycleError(f"activity already {activity.status!r}")
    item = None
    if activity.template_item_id is not None:
        item = session.scalar(
            select(ChecklistTemplateItem).where(
                ChecklistTemplateItem.tenant_id == tenant_id,
                ChecklistTemplateItem.id == activity.template_item_id,
            )
        )
    if item is not None and item.requires_document and document_file_id is None:
        raise LifecycleError("activity requires a document file reference")
    activity.status = "completed"
    activity.completed_on = completed_on
    activity.completed_by_id = completed_by_id
    activity.document_file_id = document_file_id
    session.flush()
    if onboarding_progress(session, tenant_id=tenant_id, onboarding_id=activity.onboarding_id) == Decimal(100):
        onboarding = session.scalar(
            select(EmployeeOnboarding).where(
                EmployeeOnboarding.tenant_id == tenant_id,
                EmployeeOnboarding.id == activity.onboarding_id,
            )
        )
        if onboarding is not None and onboarding.status == "in_progress":
            onboarding.status = "completed"
            onboarding.actual_completion_date = completed_on
            session.flush()
    return activity


def complete_separation(session: Session, *, tenant_id: UUID, separation_id: UUID) -> EmployeeSeparation:
    """Complete only when every clearance item is cleared."""

    separation = session.scalar(
        select(EmployeeSeparation).where(
            EmployeeSeparation.tenant_id == tenant_id,
            EmployeeSeparation.id == separation_id,
        )
    )
    if separation is None:
        raise LifecycleError("separation not found")
    if separation.status != "in_progress":
        raise LifecycleError(f"separation already {separation.status!r}")
    uncleared = session.scalar(
        select(func.count())
        .select_from(ClearanceItem)
        .where(
            ClearanceItem.tenant_id == tenant_id,
            ClearanceItem.separation_id == separation_id,
            ClearanceItem.is_cleared.is_(False),
        )
    )
    if uncleared:
        raise LifecycleError(f"{uncleared} clearance item(s) still open")
    separation.status = "completed"
    session.flush()
    return separation


__all__ = [
    "LIFECYCLE_TABLES",
    "ChecklistTemplate",
    "ChecklistTemplateItem",
    "ClearanceItem",
    "EmployeeOnboarding",
    "EmployeeSeparation",
    "EmploymentChange",
    "EmploymentChangeDetail",
    "LifecycleError",
    "OnboardingActivity",
    "complete_activity",
    "complete_separation",
    "onboarding_progress",
    "start_onboarding",
]
