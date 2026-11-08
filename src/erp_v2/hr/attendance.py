"""HR attendance — ERP-unique slice 2 (legacy: attendance, shift_type,
shift_assignment, attendance_request).

dotnet-workforce (unreleased) would own shifts/capacity generically;
until it releases, shift definitions live here as ERP-unique config.
Working/overtime/late facts are computed at marking time from the
shift, never trusted from the client.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, time, timedelta
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
    Time,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from erp_v2.hr.models import _tenant_fk


class AttendanceStatus(str, enum.Enum):
    PRESENT = "present"
    ABSENT = "absent"
    HALF_DAY = "half_day"
    ON_LEAVE = "on_leave"
    HOLIDAY = "holiday"
    WEEKLY_OFF = "weekly_off"


class ShiftType(Base, TimestampMixin):
    __tablename__ = "hr_shift_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_shift_types_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_shift_types_tenant_code"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    working_hours: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    late_entry_grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    early_exit_grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    half_day_threshold_hours: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    overtime_threshold_hours: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    break_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class ShiftAssignment(Base, TimestampMixin):
    __tablename__ = "hr_shift_assignments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_shift_assignments_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_shift_assignments_tenant_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "shift_type_id"],
            ["hr_shift_types.tenant_id", "hr_shift_types.id"],
            ondelete="RESTRICT",
            name="fk_hr_shift_assignments_tenant_shift",
        ),
        Index("ix_hr_shift_assignments_tenant_employee", "tenant_id", "employee_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    shift_type_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class Attendance(Base, TimestampMixin):
    __tablename__ = "hr_attendance"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_attendance_tenant_id_id"),
        UniqueConstraint("tenant_id", "employee_id", "attendance_date", name="uq_hr_attendance_day"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_attendance_tenant_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "shift_type_id"],
            ["hr_shift_types.tenant_id", "hr_shift_types.id"],
            ondelete="RESTRICT",
            name="fk_hr_attendance_tenant_shift",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "leave_application_id"],
            ["hr_leave_applications.tenant_id", "hr_leave_applications.id"],
            ondelete="SET NULL",
            name="fk_hr_attendance_tenant_leave",
        ),
        CheckConstraint(
            "status IN ('present','absent','half_day','on_leave','holiday','weekly_off')",
            name="ck_hr_attendance_status",
        ),
        Index("ix_hr_attendance_tenant_employee_date", "tenant_id", "employee_id", "attendance_date"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    shift_type_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    attendance_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    check_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    working_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    overtime_hours: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    late_entry_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    early_exit_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    leave_application_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    marked_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


class AttendanceRequest(Base, TimestampMixin):
    __tablename__ = "hr_attendance_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_attendance_requests_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_attendance_requests_tenant_employee",
        ),
        CheckConstraint("to_date >= from_date", name="ck_hr_attendance_requests_range"),
        CheckConstraint(
            "status IN ('draft','submitted','approved','rejected','cancelled')",
            name="ck_hr_attendance_requests_status",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="submitted")


ATTENDANCE_TABLES = (
    "hr_shift_types",
    "hr_shift_assignments",
    "hr_attendance",
    "hr_attendance_requests",
)


class AttendanceError(RuntimeError):
    """An attendance rule refusal."""


def evaluate_day(
    shift: ShiftType, *, on: date, check_in: datetime | None, check_out: datetime | None
) -> dict[str, object]:
    """Pure day evaluation: status, worked/overtime hours, late/early minutes."""

    if check_in is None:
        return {
            "status": AttendanceStatus.ABSENT.value,
            "working_hours": Decimal(0),
            "overtime_hours": Decimal(0),
            "late_entry_minutes": 0,
            "early_exit_minutes": 0,
        }
    if check_out is not None and check_out < check_in:
        raise AttendanceError("check_out is before check_in")
    shift_start = datetime.combine(on, shift.start_time, tzinfo=check_in.tzinfo)
    shift_end = datetime.combine(on, shift.end_time, tzinfo=check_in.tzinfo)
    if shift_end <= shift_start:  # overnight shift
        shift_end += timedelta(days=1)
    late = max(
        0,
        int((check_in - shift_start).total_seconds() // 60) - shift.late_entry_grace_minutes,
    )
    early = 0
    worked = Decimal("0")
    if check_out is not None:
        early = max(
            0,
            int((shift_end - check_out).total_seconds() // 60) - shift.early_exit_grace_minutes,
        )
        span_minutes = Decimal(str((check_out - check_in).total_seconds() / 60))
        worked = max(
            Decimal("0"),
            (span_minutes - Decimal(shift.break_duration_minutes)) / Decimal(60),
        ).quantize(Decimal("0.01"))
    overtime = Decimal("0")
    if shift.overtime_threshold_hours is not None and worked > shift.overtime_threshold_hours:
        overtime = (worked - shift.overtime_threshold_hours).quantize(Decimal("0.01"))
    status = AttendanceStatus.PRESENT
    if shift.half_day_threshold_hours is not None and check_out is not None and worked < shift.half_day_threshold_hours:
        status = AttendanceStatus.HALF_DAY
    return {
        "status": status.value,
        "working_hours": worked,
        "overtime_hours": overtime,
        "late_entry_minutes": late,
        "early_exit_minutes": early,
    }


def mark_attendance(
    session: Session,
    *,
    tenant_id: UUID,
    employee_id: UUID,
    on: date,
    check_in: datetime | None,
    check_out: datetime | None,
    marked_by_id: UUID | None = None,
    leave_application_id: UUID | None = None,
) -> Attendance:
    """Create/replace the day's attendance from the assigned shift."""

    assignment = session.scalar(
        select(ShiftAssignment).where(
            ShiftAssignment.tenant_id == tenant_id,
            ShiftAssignment.employee_id == employee_id,
            ShiftAssignment.start_date <= on,
            (ShiftAssignment.end_date.is_(None)) | (ShiftAssignment.end_date >= on),
            ShiftAssignment.is_active.is_(True),
        )
    )
    shift = None
    if assignment is not None:
        shift = session.scalar(
            select(ShiftType).where(ShiftType.tenant_id == tenant_id, ShiftType.id == assignment.shift_type_id)
        )
    if shift is None and check_in is not None:
        raise AttendanceError("no active shift assignment for the employee on that date")
    facts: dict[str, object] = (
        evaluate_day(shift, on=on, check_in=check_in, check_out=check_out)
        if shift is not None
        else {
            "status": AttendanceStatus.ON_LEAVE.value if leave_application_id else AttendanceStatus.ABSENT.value,
            "working_hours": Decimal(0),
            "overtime_hours": Decimal(0),
            "late_entry_minutes": 0,
            "early_exit_minutes": 0,
        }
    )
    existing = session.scalar(
        select(Attendance).where(
            Attendance.tenant_id == tenant_id,
            Attendance.employee_id == employee_id,
            Attendance.attendance_date == on,
        )
    )
    row = existing or Attendance(tenant_id=tenant_id, employee_id=employee_id, attendance_date=on)
    row.shift_type_id = shift.id if shift else None
    row.check_in = check_in
    row.check_out = check_out
    row.leave_application_id = leave_application_id
    row.marked_by_id = marked_by_id
    for key, value in facts.items():
        setattr(row, key, value)
    if existing is None:
        session.add(row)
    session.flush()
    return row


__all__ = [
    "ATTENDANCE_TABLES",
    "Attendance",
    "AttendanceError",
    "AttendanceRequest",
    "AttendanceStatus",
    "ShiftAssignment",
    "ShiftType",
    "evaluate_day",
    "mark_attendance",
]
