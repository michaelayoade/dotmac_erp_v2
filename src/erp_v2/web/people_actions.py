"""Leave write flows for the People workspace.

Thin orchestration over owners: day counting and balance rules stay
in `erp_v2.hr.service`, document numbers come from dotmac-numbering,
identity/employee resolution from kernel Party + dotmac-people.
Segregation of duties beyond the facet admission permission arrives
with the approvals-module integration; every decision stamps the
acting party.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from uuid import UUID

from dotmac_kernel.cache import TenantScope
from dotmac_kernel.models import Party, Tenant
from dotmac_numbering.models import NumberSeries
from dotmac_numbering.service import SeriesConfiguration, allocate, configure_series
from dotmac_people.models import Employee
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.hr import service as leave_service
from erp_v2.hr.models import Holiday, HolidayList, LeaveApplication, LeaveType

LEAVE_SERIES_CODE = "leave_application"


class PeopleActionError(RuntimeError):
    """A user-facing People action refusal (rendered, never 500)."""


def numbering_allocator(db: Session, tenant: Tenant) -> Callable[[], str]:
    """Allocate leave numbers, configuring the series on first use."""

    def _allocate() -> str:
        scope = TenantScope(tenant.id)
        exists = db.scalar(
            select(NumberSeries).where(
                NumberSeries.tenant_id == tenant.id,
                NumberSeries.series_code == LEAVE_SERIES_CODE,
            )
        )
        if exists is None:
            configure_series(
                db,
                scope=scope,
                configuration=SeriesConfiguration(
                    series_code=LEAVE_SERIES_CODE,
                    prefix="LV",
                    include_year=True,
                    reset_policy="yearly",
                    min_digits=4,
                ),
            )
        result = allocate(
            db,
            scope=scope,
            series_code=LEAVE_SERIES_CODE,
            reference_date=date.today(),
            idempotency_key=str(uuid.uuid4()),
        )
        return result.formatted_number

    return _allocate


def _employee_for_party(db: Session, tenant: Tenant, party: Party) -> Employee:
    employee = db.scalar(select(Employee).where(Employee.tenant_id == tenant.id, Employee.party_id == party.id))
    if employee is None:
        raise PeopleActionError("no employee record is linked to your account")
    return employee


def _holiday_context(db: Session, tenant: Tenant) -> tuple[set[date], tuple[int, ...]]:
    holiday_list = db.scalar(
        select(HolidayList).where(HolidayList.tenant_id == tenant.id, HolidayList.is_default.is_(True))
    )
    if holiday_list is None:
        return set(), (6, 7)
    days = set(
        db.scalars(
            select(Holiday.holiday_date).where(
                Holiday.tenant_id == tenant.id,
                Holiday.holiday_list_id == holiday_list.id,
            )
        )
    )
    off = tuple(int(part) for part in holiday_list.weekly_off.split(",") if part.strip().isdigit())
    return days, off or (6, 7)


def apply_for_leave(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    leave_type_code: str,
    from_date: date,
    to_date: date,
    allocate_number: Callable[[], str],
    half_day: bool = False,
    reason: str | None = None,
) -> LeaveApplication:
    employee = _employee_for_party(db, tenant, party)
    leave_type = db.scalar(select(LeaveType).where(LeaveType.tenant_id == tenant.id, LeaveType.code == leave_type_code))
    if leave_type is None:
        raise PeopleActionError(f"unknown leave type {leave_type_code!r}")
    holidays, weekly_off = _holiday_context(db, tenant)
    try:
        days = leave_service.compute_leave_days(
            from_date,
            to_date,
            half_day=half_day,
            holidays=holidays,
            weekly_off=weekly_off,
            include_holidays=leave_type.include_holidays,
        )
        return leave_service.apply_leave(
            db,
            tenant_id=tenant.id,
            number=allocate_number(),
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            from_date=from_date,
            to_date=to_date,
            total_leave_days=days,
            half_day=half_day,
            reason=reason,
        )
    except leave_service.LeaveError as exc:
        raise PeopleActionError(str(exc)) from exc


def decide_leave(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    application_id: UUID,
    approve: bool,
    rejection_reason: str | None = None,
) -> LeaveApplication:
    try:
        return leave_service.decide_leave(
            db,
            tenant_id=tenant.id,
            application_id=application_id,
            approve=approve,
            decided_by_id=party.id,
            rejection_reason=rejection_reason,
        )
    except leave_service.LeaveError as exc:
        raise PeopleActionError(str(exc)) from exc


def cancel_leave(db: Session, *, tenant: Tenant, party: Party, application_id: UUID) -> LeaveApplication:
    application = db.scalar(
        select(LeaveApplication).where(
            LeaveApplication.tenant_id == tenant.id,
            LeaveApplication.id == application_id,
        )
    )
    if application is None:
        raise PeopleActionError("leave application not found")
    employee = _employee_for_party(db, tenant, party)
    if application.employee_id != employee.id:
        raise PeopleActionError("only the applicant can cancel this application")
    try:
        return leave_service.cancel_leave(db, tenant_id=tenant.id, application_id=application_id)
    except leave_service.LeaveError as exc:
        raise PeopleActionError(str(exc)) from exc


__all__ = [
    "LEAVE_SERIES_CODE",
    "PeopleActionError",
    "apply_for_leave",
    "cancel_leave",
    "decide_leave",
    "numbering_allocator",
]
