"""HR feedback/appeals + discipline: guarded consequence flows."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import dotmac_people.models as people_models
import pytest
import sqlalchemy as sa
from dotmac_kernel.models import Base, Tenant
from sqlalchemy.orm import Session

from erp_v2.hr import appraisals as ap
from erp_v2.hr import attendance as att
from erp_v2.hr import discipline as dc
from erp_v2.hr import feedback as fb
from erp_v2.hr import models as hr


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
    ] + [
        Base.metadata.tables[n]
        for n in (
            hr.HR_TABLES + att.ATTENDANCE_TABLES + ap.APPRAISAL_TABLES + fb.FEEDBACK_TABLES + dc.DISCIPLINE_TABLES
        )
    ]
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
                date_of_joining=date(2025, 1, 1),
            )
        )
        s.flush()
        yield s, tenant_id, employee_id


def _completed_appraisal(s, tenant_id, employee_id):
    cycle = ap.AppraisalCycle(
        tenant_id=tenant_id,
        code="2026",
        name="2026",
        review_period_start=date(2026, 1, 1),
        review_period_end=date(2026, 12, 31),
        status="open",
    )
    template = ap.AppraisalTemplate(tenant_id=tenant_id, code="STD", name="Standard")
    kra = ap.Kra(tenant_id=tenant_id, code="K1", name="Delivery")
    s.add_all([cycle, template, kra])
    s.flush()
    appraisal = ap.Appraisal(
        tenant_id=tenant_id,
        employee_id=employee_id,
        cycle_id=cycle.id,
        template_id=template.id,
        status="calibrated",
        final_rating=Decimal(3),
    )
    s.add(appraisal)
    s.flush()
    s.add(
        ap.AppraisalKraScore(
            tenant_id=tenant_id,
            appraisal_id=appraisal.id,
            kra_id=kra.id,
            weightage=Decimal(100),
            final_rating=Decimal(3),
        )
    )
    s.flush()
    return ap.advance(s, tenant_id=tenant_id, appraisal_id=appraisal.id, to_status=ap.AppraisalStatus.COMPLETED)


def test_appeal_upheld_adjusts_rating_once(session) -> None:
    s, tenant_id, employee_id = session
    appraisal = _completed_appraisal(s, tenant_id, employee_id)
    fb.submit_feedback(
        s, tenant_id=tenant_id, appraisal_id=appraisal.id, feedback_type="peer", overall_rating=Decimal(4)
    )
    appeal = fb.file_appeal(
        s, tenant_id=tenant_id, appraisal_id=appraisal.id, filed_date=date(2027, 1, 10), reason="evidence missed"
    )
    with pytest.raises(fb.FeedbackError):  # second open appeal refused
        fb.file_appeal(s, tenant_id=tenant_id, appraisal_id=appraisal.id, filed_date=date(2027, 1, 11), reason="again")
    fb.resolve_appeal(
        s,
        tenant_id=tenant_id,
        appeal_id=appeal.id,
        upheld=True,
        resolution_date=date(2027, 2, 1),
        adjusted_rating=Decimal("3.5"),
    )
    assert appraisal.final_rating == Decimal("3.5")
    actions = s.execute(sa.select(fb.AppraisalOutcomeAction)).scalars().all()
    assert len(actions) == 1 and "-> 3.5" in (actions[0].description or "")
    with pytest.raises(fb.FeedbackError):  # double resolve refused
        fb.resolve_appeal(
            s,
            tenant_id=tenant_id,
            appeal_id=appeal.id,
            upheld=True,
            resolution_date=date(2027, 2, 2),
            adjusted_rating=Decimal(4),
        )


def test_discipline_flow_and_deadline(session) -> None:
    s, tenant_id, employee_id = session
    case = dc.DisciplinaryCase(
        tenant_id=tenant_id,
        number="DC-1",
        employee_id=employee_id,
        violation_type="misconduct",
        severity="major",
        subject="Query",
        reported_date=date(2026, 3, 1),
        appeal_deadline=date(2026, 4, 1),
    )
    s.add(case)
    s.flush()
    with pytest.raises(dc.DisciplineError):  # cannot skip to decided
        dc.transition_case(s, tenant_id=tenant_id, case_id=case.id, to_status=dc.CaseStatus.DECIDED)
    dc.transition_case(s, tenant_id=tenant_id, case_id=case.id, to_status=dc.CaseStatus.QUERIED)
    dc.respond_to_case(s, tenant_id=tenant_id, case_id=case.id, response_text="my side")
    dc.transition_case(
        s,
        tenant_id=tenant_id,
        case_id=case.id,
        to_status=dc.CaseStatus.DECIDED,
        on=date(2026, 3, 20),
        decision_summary="warning",
    )
    with pytest.raises(dc.DisciplineError, match="deadline"):
        dc.transition_case(
            s, tenant_id=tenant_id, case_id=case.id, to_status=dc.CaseStatus.APPEALED, on=date(2026, 4, 2)
        )
    dc.transition_case(
        s,
        tenant_id=tenant_id,
        case_id=case.id,
        to_status=dc.CaseStatus.APPEALED,
        on=date(2026, 3, 25),
        appeal_reason="new evidence",
    )
    dc.transition_case(
        s,
        tenant_id=tenant_id,
        case_id=case.id,
        to_status=dc.CaseStatus.CLOSED,
        on=date(2026, 4, 10),
        appeal_decision="upheld original",
    )
    assert case.status == "closed" and case.closed_date == date(2026, 4, 10)

    grievance = dc.Grievance(
        tenant_id=tenant_id, number="GR-1", employee_id=employee_id, category="pay", subject="overtime"
    )
    s.add(grievance)
    s.flush()
    dc.resolve_grievance(
        s, tenant_id=tenant_id, grievance_id=grievance.id, resolution="paid", resolution_date=date(2026, 4, 12)
    )
    assert grievance.status == "resolved"
