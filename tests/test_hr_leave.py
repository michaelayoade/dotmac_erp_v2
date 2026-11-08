"""HR leave slice: day counting, balance consumption, guarded decisions."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import dotmac_people.models as people_models
import pytest
import sqlalchemy as sa
from dotmac_kernel.models import Base, Tenant
from sqlalchemy.orm import Session

from erp_v2.hr import models as hr
from erp_v2.hr.service import LeaveError, apply_leave, cancel_leave, compute_leave_days, decide_leave


def test_compute_leave_days_skips_weekly_off_and_holidays() -> None:
    # Mon 2026-01-05 .. Sun 2026-01-11, holiday on Wed.
    assert compute_leave_days(date(2026, 1, 5), date(2026, 1, 11), holidays={date(2026, 1, 7)}) == Decimal(4)
    assert compute_leave_days(
        date(2026, 1, 5),
        date(2026, 1, 11),
        holidays={date(2026, 1, 7)},
        include_holidays=True,
    ) == Decimal(5)
    assert compute_leave_days(date(2026, 1, 5), date(2026, 1, 5), half_day=True) == Decimal("0.5")
    with pytest.raises(LeaveError):
        compute_leave_days(date(2026, 1, 6), date(2026, 1, 5))


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_people"))
    tables = [
        Tenant.__table__,
        people_models.Department.__table__,
        people_models.Designation.__table__,
        people_models.EmploymentType.__table__,
        people_models.Employee.__table__,
    ] + [Base.metadata.tables[n] for n in hr.HR_TABLES]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as s:
        tenant_id = uuid.uuid4()
        employee_id = uuid.uuid4()
        s.add(Tenant(id=tenant_id, slug="acme", name="Acme"))
        s.add(
            people_models.Employee(
                id=employee_id,
                tenant_id=tenant_id,
                party_id=uuid.uuid4(),
                employee_code="E001",
                date_of_joining=date(2025, 1, 1),
            )
        )
        leave_type = hr.LeaveType(tenant_id=tenant_id, code="ANNUAL", name="Annual")
        s.add(leave_type)
        s.flush()
        allocation = hr.LeaveAllocation(
            tenant_id=tenant_id,
            employee_id=employee_id,
            leave_type_id=leave_type.id,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 12, 31),
            new_leaves_allocated=Decimal(20),
        )
        s.add(allocation)
        s.flush()
        yield s, tenant_id, employee_id, leave_type.id, allocation


def test_apply_decide_cancel_balance_flow(session) -> None:
    s, tenant_id, employee_id, leave_type_id, allocation = session
    app = apply_leave(
        s,
        tenant_id=tenant_id,
        number="LV-1",
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        from_date=date(2026, 2, 2),
        to_date=date(2026, 2, 6),
        total_leave_days=Decimal(5),
    )
    assert allocation.balance == Decimal(20)  # not consumed until approval
    decide_leave(s, tenant_id=tenant_id, application_id=app.id, approve=True, decided_by_id=uuid.uuid4())
    assert allocation.balance == Decimal(15)
    with pytest.raises(LeaveError):  # double-decide refused
        decide_leave(
            s,
            tenant_id=tenant_id,
            application_id=app.id,
            approve=True,
            decided_by_id=uuid.uuid4(),
        )
    cancel_leave(s, tenant_id=tenant_id, application_id=app.id)
    assert allocation.balance == Decimal(20)  # days returned


def test_apply_refuses_insufficient_balance(session) -> None:
    s, tenant_id, employee_id, leave_type_id, _ = session
    with pytest.raises(LeaveError, match="insufficient"):
        apply_leave(
            s,
            tenant_id=tenant_id,
            number="LV-2",
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            from_date=date(2026, 3, 2),
            to_date=date(2026, 3, 31),
            total_leave_days=Decimal(21),
        )
