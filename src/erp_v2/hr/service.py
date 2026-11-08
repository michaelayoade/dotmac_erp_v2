"""HR leave service — flush-only, caller's transaction (module convention).

Balance consumption happens exactly once, at approval, guarded by the
application's current status; cancelling an approved application
returns the days. Re-approving or deciding a terminal application is
refused, never a no-op, so a double-decide cannot double-consume.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.hr.models import (
    LeaveAllocation,
    LeaveApplication,
    LeaveApplicationStatus,
)


class LeaveError(RuntimeError):
    """A leave rule refusal (balance, overlap of state, bad range)."""


def compute_leave_days(
    from_date: date,
    to_date: date,
    *,
    half_day: bool = False,
    holidays: Iterable[date] = (),
    weekly_off: Iterable[int] = (6, 7),
    include_holidays: bool = False,
) -> Decimal:
    """Chargeable days in an inclusive range (legacy semantics)."""

    if to_date < from_date:
        raise LeaveError("to_date is before from_date")
    if half_day:
        if from_date != to_date:
            raise LeaveError("half_day requires a single-day range")
        return Decimal("0.5")
    holiday_set = set(holidays)
    off_set = set(weekly_off)
    days = Decimal(0)
    current = from_date
    while current <= to_date:
        if current.isoweekday() in off_set:
            pass
        elif not include_holidays and current in holiday_set:
            pass
        else:
            days += 1
        current += timedelta(days=1)
    return days


def _allocation_for(
    session: Session, *, tenant_id: UUID, employee_id: UUID, leave_type_id: UUID, on: date
) -> LeaveAllocation:
    allocation = session.scalar(
        select(LeaveAllocation).where(
            LeaveAllocation.tenant_id == tenant_id,
            LeaveAllocation.employee_id == employee_id,
            LeaveAllocation.leave_type_id == leave_type_id,
            LeaveAllocation.from_date <= on,
            LeaveAllocation.to_date >= on,
            LeaveAllocation.is_active.is_(True),
        )
    )
    if allocation is None:
        raise LeaveError("no active leave allocation covers the requested dates")
    return allocation


def apply_leave(
    session: Session,
    *,
    tenant_id: UUID,
    number: str,
    employee_id: UUID,
    leave_type_id: UUID,
    from_date: date,
    to_date: date,
    total_leave_days: Decimal,
    half_day: bool = False,
    reason: str | None = None,
) -> LeaveApplication:
    """Create a SUBMITTED application after a balance pre-check."""

    if total_leave_days <= 0:
        raise LeaveError("total_leave_days must be positive")
    allocation = _allocation_for(
        session,
        tenant_id=tenant_id,
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        on=from_date,
    )
    if allocation.balance < total_leave_days:
        raise LeaveError(f"insufficient leave balance: {allocation.balance} < {total_leave_days}")
    application = LeaveApplication(
        tenant_id=tenant_id,
        number=number,
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        allocation_id=allocation.id,
        from_date=from_date,
        to_date=to_date,
        half_day=half_day,
        total_leave_days=total_leave_days,
        reason=reason,
        status=LeaveApplicationStatus.SUBMITTED.value,
    )
    session.add(application)
    session.flush()
    return application


def decide_leave(
    session: Session,
    *,
    tenant_id: UUID,
    application_id: UUID,
    approve: bool,
    decided_by_id: UUID,
    rejection_reason: str | None = None,
) -> LeaveApplication:
    """Approve (consume balance) or reject a SUBMITTED application."""

    application = session.scalar(
        select(LeaveApplication).where(
            LeaveApplication.tenant_id == tenant_id,
            LeaveApplication.id == application_id,
        )
    )
    if application is None:
        raise LeaveError("leave application not found")
    if application.status != LeaveApplicationStatus.SUBMITTED.value:
        raise LeaveError(f"cannot decide an application in status {application.status!r}")
    application.decided_by_id = decided_by_id
    if not approve:
        application.status = LeaveApplicationStatus.REJECTED.value
        application.rejection_reason = rejection_reason
        session.flush()
        return application
    allocation = session.scalar(
        select(LeaveAllocation).where(
            LeaveAllocation.tenant_id == tenant_id,
            LeaveAllocation.id == application.allocation_id,
        )
    )
    if allocation is None or allocation.balance < application.total_leave_days:
        raise LeaveError("insufficient leave balance at approval")
    allocation.leaves_used += application.total_leave_days
    application.status = LeaveApplicationStatus.APPROVED.value
    session.flush()
    return application


def cancel_leave(session: Session, *, tenant_id: UUID, application_id: UUID) -> LeaveApplication:
    """Cancel a submitted/approved application, returning consumed days."""

    application = session.scalar(
        select(LeaveApplication).where(
            LeaveApplication.tenant_id == tenant_id,
            LeaveApplication.id == application_id,
        )
    )
    if application is None:
        raise LeaveError("leave application not found")
    if application.status == LeaveApplicationStatus.APPROVED.value:
        allocation = session.scalar(
            select(LeaveAllocation).where(
                LeaveAllocation.tenant_id == tenant_id,
                LeaveAllocation.id == application.allocation_id,
            )
        )
        if allocation is not None:
            allocation.leaves_used -= application.total_leave_days
    elif application.status != LeaveApplicationStatus.SUBMITTED.value:
        raise LeaveError(f"cannot cancel an application in status {application.status!r}")
    application.status = LeaveApplicationStatus.CANCELLED.value
    session.flush()
    return application


__all__ = ["LeaveError", "apply_leave", "cancel_leave", "compute_leave_days", "decide_leave"]
