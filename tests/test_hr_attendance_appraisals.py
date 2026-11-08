"""HR attendance + appraisals: evaluation math and guarded flows."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from decimal import Decimal

import dotmac_people.models as people_models
import pytest
import sqlalchemy as sa
from dotmac_kernel.models import Base, Tenant
from sqlalchemy.orm import Session

from erp_v2.hr import appraisals as ap
from erp_v2.hr import attendance as att
from erp_v2.hr import models as hr


def _shift(**kw) -> att.ShiftType:
    base = {
        "tenant_id": uuid.uuid4(),
        "code": "DAY",
        "name": "Day",
        "start_time": time(9, 0),
        "end_time": time(17, 0),
        "working_hours": Decimal(8),
        "late_entry_grace_minutes": 10,
        "early_exit_grace_minutes": 0,
        "half_day_threshold_hours": Decimal(4),
        "overtime_threshold_hours": Decimal(8),
        "break_duration_minutes": 60,
    }
    base.update(kw)
    return att.ShiftType(**base)


def test_evaluate_day_late_overtime_halfday() -> None:
    from datetime import date

    shift = _shift()
    tz = timezone.utc
    on = date(2026, 1, 5)
    facts = att.evaluate_day(
        shift,
        on=on,
        check_in=datetime(2026, 1, 5, 9, 25, tzinfo=tz),
        check_out=datetime(2026, 1, 5, 19, 0, tzinfo=tz),
    )
    assert facts["late_entry_minutes"] == 15  # 25 late - 10 grace
    assert facts["working_hours"] == Decimal("8.58")  # 9h35 - 60 break
    assert facts["overtime_hours"] == Decimal("0.58")
    half = att.evaluate_day(
        shift,
        on=on,
        check_in=datetime(2026, 1, 5, 9, 0, tzinfo=tz),
        check_out=datetime(2026, 1, 5, 12, 0, tzinfo=tz),
    )
    assert half["status"] == "half_day"
    assert att.evaluate_day(shift, on=on, check_in=None, check_out=None)["status"] == "absent"


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
    ] + [Base.metadata.tables[n] for n in hr.HR_TABLES + att.ATTENDANCE_TABLES + ap.APPRAISAL_TABLES]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as s:
        tenant_id, employee_id = uuid.uuid4(), uuid.uuid4()
        s.add(Tenant(id=tenant_id, slug="acme", name="Acme"))
        s.add(
            people_models.Employee(
                id=employee_id,
                tenant_id=tenant_id,
                party_id=uuid.uuid4(),
                employee_code="E001",
                date_of_joining=__import__("datetime").date(2025, 1, 1),
            )
        )
        s.flush()
        yield s, tenant_id, employee_id


def test_mark_attendance_and_appraisal_completion(session) -> None:
    from datetime import date

    s, tenant_id, employee_id = session
    shift = _shift(tenant_id=tenant_id)
    s.add(shift)
    s.flush()
    s.add(
        att.ShiftAssignment(
            tenant_id=tenant_id,
            employee_id=employee_id,
            shift_type_id=shift.id,
            start_date=date(2026, 1, 1),
        )
    )
    s.flush()
    row = att.mark_attendance(
        s,
        tenant_id=tenant_id,
        employee_id=employee_id,
        on=date(2026, 1, 5),
        check_in=datetime(2026, 1, 5, 9, 5, tzinfo=timezone.utc),
        check_out=datetime(2026, 1, 5, 17, 0, tzinfo=timezone.utc),
    )
    assert row.status == "present" and row.late_entry_minutes == 0

    cycle = ap.AppraisalCycle(
        tenant_id=tenant_id,
        code="2026",
        name="2026 Annual",
        review_period_start=date(2026, 1, 1),
        review_period_end=date(2026, 12, 31),
        status="open",
    )
    template = ap.AppraisalTemplate(tenant_id=tenant_id, code="STD", name="Standard")
    kra = ap.Kra(tenant_id=tenant_id, code="DELIVERY", name="Delivery")
    s.add_all([cycle, template, kra])
    s.flush()
    appraisal = ap.Appraisal(tenant_id=tenant_id, employee_id=employee_id, cycle_id=cycle.id, template_id=template.id)
    s.add(appraisal)
    s.flush()
    s.add(
        ap.AppraisalKraScore(
            tenant_id=tenant_id,
            appraisal_id=appraisal.id,
            kra_id=kra.id,
            weightage=Decimal(100),
            final_rating=Decimal(4),
        )
    )
    s.flush()
    for step in (
        ap.AppraisalStatus.SELF_SUBMITTED,
        ap.AppraisalStatus.MANAGER_REVIEWED,
        ap.AppraisalStatus.CALIBRATED,
    ):
        ap.advance(s, tenant_id=tenant_id, appraisal_id=appraisal.id, to_status=step)
    done = ap.advance(s, tenant_id=tenant_id, appraisal_id=appraisal.id, to_status=ap.AppraisalStatus.COMPLETED)
    assert done.final_score == Decimal("4.00") and done.completed_on is not None
    with pytest.raises(ap.AppraisalError):
        ap.advance(s, tenant_id=tenant_id, appraisal_id=appraisal.id, to_status=ap.AppraisalStatus.COMPLETED)
