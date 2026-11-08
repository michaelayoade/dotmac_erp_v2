"""HR leave models — first ERP-unique (`e2`) slice.

Shapes follow the legacy ERP leave tables (leave_type,
leave_allocation, leave_application, holiday_list, holiday) renamed
to module conventions, so the transform is column mapping, not
redesign. Employee references are composite tenant FKs into
`mod_people.employees`; balances live on the allocation row exactly
as legacy kept them. Approval DECISIONS belong to dotmac-approvals
when the web surface lands; the status transitions here are the
subject owner's guarded consequences (per the approvals boundary).
"""

from __future__ import annotations

import enum
from datetime import date
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.models import Base, Tenant, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column


class LeaveApplicationStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


def _tenant_fk() -> Mapped[UUID]:
    return mapped_column(Uuid(), ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"), nullable=False)


class LeaveType(Base, TimestampMixin):
    __tablename__ = "hr_leave_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_leave_types_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_leave_types_tenant_code"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_days_per_year: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    max_continuous_days: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    allow_carry_forward: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    max_carry_forward_days: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    allow_encashment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    is_lwp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    include_holidays: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class HolidayList(Base, TimestampMixin):
    __tablename__ = "hr_holiday_lists"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_holiday_lists_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_holiday_lists_tenant_code"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    # ISO weekday numbers (1=Mon..7=Sun) as comma list, e.g. "6,7".
    weekly_off: Mapped[str] = mapped_column(String(20), nullable=False, default="6,7")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class Holiday(Base, TimestampMixin):
    __tablename__ = "hr_holidays"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_holidays_tenant_id_id"),
        UniqueConstraint("tenant_id", "holiday_list_id", "holiday_date", name="uq_hr_holidays_list_date"),
        ForeignKeyConstraint(
            ["tenant_id", "holiday_list_id"],
            ["hr_holiday_lists.tenant_id", "hr_holiday_lists.id"],
            ondelete="CASCADE",
            name="fk_hr_holidays_tenant_list",
        ),
        Index("ix_hr_holidays_tenant_list", "tenant_id", "holiday_list_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    holiday_list_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public_holiday: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())
    is_optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())


class LeaveAllocation(Base, TimestampMixin):
    __tablename__ = "hr_leave_allocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_leave_allocations_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_leave_allocations_tenant_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["hr_leave_types.tenant_id", "hr_leave_types.id"],
            ondelete="RESTRICT",
            name="fk_hr_leave_allocations_tenant_type",
        ),
        CheckConstraint("to_date >= from_date", name="ck_hr_leave_allocations_range"),
        Index("ix_hr_leave_allocations_tenant_employee", "tenant_id", "employee_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    leave_type_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    new_leaves_allocated: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    carry_forward_leaves: Mapped[Decimal] = mapped_column(
        Numeric(7, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    leaves_used: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False, default=Decimal(0), server_default="0")
    leaves_encashed: Mapped[Decimal] = mapped_column(
        Numeric(7, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    leaves_expired: Mapped[Decimal] = mapped_column(
        Numeric(7, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())

    @property
    def balance(self) -> Decimal:
        allocated = self.new_leaves_allocated + self.carry_forward_leaves
        return allocated - self.leaves_used - self.leaves_encashed - self.leaves_expired


class LeaveApplication(Base, TimestampMixin):
    __tablename__ = "hr_leave_applications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_leave_applications_tenant_id_id"),
        UniqueConstraint("tenant_id", "number", name="uq_hr_leave_applications_tenant_number"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_leave_applications_tenant_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["hr_leave_types.tenant_id", "hr_leave_types.id"],
            ondelete="RESTRICT",
            name="fk_hr_leave_applications_tenant_type",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "allocation_id"],
            ["hr_leave_allocations.tenant_id", "hr_leave_allocations.id"],
            ondelete="RESTRICT",
            name="fk_hr_leave_applications_tenant_allocation",
        ),
        CheckConstraint("to_date >= from_date", name="ck_hr_leave_applications_range"),
        CheckConstraint(
            "status IN ('draft','submitted','approved','rejected','cancelled')",
            name="ck_hr_leave_applications_status",
        ),
        Index("ix_hr_leave_applications_tenant_employee", "tenant_id", "employee_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    leave_type_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    allocation_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    total_leave_days: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=LeaveApplicationStatus.DRAFT.value)
    decided_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


HR_TABLES = (
    "hr_leave_types",
    "hr_holiday_lists",
    "hr_holidays",
    "hr_leave_allocations",
    "hr_leave_applications",
)

__all__ = [
    "HR_TABLES",
    "Holiday",
    "HolidayList",
    "LeaveAllocation",
    "LeaveApplication",
    "LeaveApplicationStatus",
    "LeaveType",
]
