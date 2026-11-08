"""Recruitment, lifecycle and training slices: guarded flows end to end."""

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
from erp_v2.hr import lifecycle as life
from erp_v2.hr import models as hr
from erp_v2.hr import recruitment as rec
from erp_v2.hr import training as tr

_ALL = (
    hr.HR_TABLES
    + att.ATTENDANCE_TABLES
    + ap.APPRAISAL_TABLES
    + fb.FEEDBACK_TABLES
    + dc.DISCIPLINE_TABLES
    + rec.RECRUITMENT_TABLES
    + life.LIFECYCLE_TABLES
    + tr.TRAINING_TABLES
)


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
    ] + [Base.metadata.tables[n] for n in _ALL]
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


def test_recruitment_offer_consumes_slot_once(session) -> None:
    s, tenant_id, _ = session
    opening = rec.JobOpening(
        tenant_id=tenant_id,
        code="ENG-1",
        title="Engineer",
        number_of_positions=1,
        status="open",
    )
    s.add(opening)
    s.flush()
    applicants = []
    for i in (1, 2):
        a = rec.JobApplicant(
            tenant_id=tenant_id,
            application_number=f"A{i}",
            job_opening_id=opening.id,
            first_name="Ada",
            last_name=f"N{i}",
            email=f"a{i}@x.io",
            applied_on=date(2026, 1, 5),
        )
        s.add(a)
        applicants.append(a)
    s.flush()
    offers = []
    for a in applicants:
        for step in (rec.ApplicantStatus.SCREENING, rec.ApplicantStatus.INTERVIEW, rec.ApplicantStatus.OFFER):
            rec.advance_applicant(s, tenant_id=tenant_id, applicant_id=a.id, to_status=step)
        o = rec.JobOffer(
            tenant_id=tenant_id,
            number=f"OF-{a.application_number}",
            applicant_id=a.id,
            job_opening_id=opening.id,
            offer_date=date(2026, 2, 1),
            valid_until=date(2026, 2, 15),
            base_salary=Decimal(100000),
            currency_code="NGN",
            status="extended",
        )
        s.add(o)
        offers.append(o)
    s.flush()
    rec.respond_offer(s, tenant_id=tenant_id, offer_id=offers[0].id, accept=True, on=date(2026, 2, 3))
    assert applicants[0].status == "hired" and opening.status == "closed"
    with pytest.raises(rec.RecruitmentError, match="filled"):
        rec.respond_offer(s, tenant_id=tenant_id, offer_id=offers[1].id, accept=True, on=date(2026, 2, 3))
    with pytest.raises(rec.RecruitmentError, match="expired"):
        rec.respond_offer(s, tenant_id=tenant_id, offer_id=offers[1].id, accept=False, on=date(2026, 2, 16))


def test_onboarding_materializes_and_completes(session) -> None:
    s, tenant_id, employee_id = session
    template = life.ChecklistTemplate(tenant_id=tenant_id, code="ONB", name="Onboarding", template_type="onboarding")
    s.add(template)
    s.flush()
    s.add_all(
        [
            life.ChecklistTemplateItem(
                tenant_id=tenant_id,
                template_id=template.id,
                name="Contract signed",
                sequence=1,
                days_from_start=0,
                requires_document=True,
            ),
            life.ChecklistTemplateItem(
                tenant_id=tenant_id, template_id=template.id, name="Laptop issued", sequence=2, days_from_start=1
            ),
        ]
    )
    s.flush()
    ob = life.start_onboarding(
        s,
        tenant_id=tenant_id,
        employee_id=employee_id,
        template_id=template.id,
        date_of_joining=date(2026, 3, 2),
    )
    assert life.onboarding_progress(s, tenant_id=tenant_id, onboarding_id=ob.id) == Decimal(0)
    acts = list(s.scalars(sa.select(life.OnboardingActivity).order_by(life.OnboardingActivity.sequence)))
    assert len(acts) == 2 and acts[1].due_date == date(2026, 3, 3)
    with pytest.raises(life.LifecycleError, match="document"):
        life.complete_activity(s, tenant_id=tenant_id, activity_id=acts[0].id, completed_on=date(2026, 3, 2))
    life.complete_activity(
        s, tenant_id=tenant_id, activity_id=acts[0].id, completed_on=date(2026, 3, 2), document_file_id=uuid.uuid4()
    )
    assert life.onboarding_progress(s, tenant_id=tenant_id, onboarding_id=ob.id) == Decimal(50)
    life.complete_activity(s, tenant_id=tenant_id, activity_id=acts[1].id, completed_on=date(2026, 3, 3))
    assert ob.status == "completed" and ob.actual_completion_date == date(2026, 3, 3)


def test_separation_needs_full_clearance_and_training_capacity(session) -> None:
    s, tenant_id, employee_id = session
    sep = life.EmployeeSeparation(
        tenant_id=tenant_id,
        employee_id=employee_id,
        separation_type="resignation",
        separation_date=date(2026, 5, 29),
    )
    s.add(sep)
    s.flush()
    item = life.ClearanceItem(tenant_id=tenant_id, separation_id=sep.id, category="IT", description="Return laptop")
    s.add(item)
    s.flush()
    with pytest.raises(life.LifecycleError, match="still open"):
        life.complete_separation(s, tenant_id=tenant_id, separation_id=sep.id)
    item.is_cleared = True
    s.flush()
    life.complete_separation(s, tenant_id=tenant_id, separation_id=sep.id)
    assert sep.status == "completed"

    program = tr.TrainingProgram(tenant_id=tenant_id, code="SAFE", name="Safety")
    s.add(program)
    s.flush()
    event = tr.TrainingEvent(
        tenant_id=tenant_id,
        program_id=program.id,
        name="Safety Jan",
        start_date=date(2026, 1, 12),
        end_date=date(2026, 1, 12),
        max_attendees=1,
    )
    s.add(event)
    s.flush()
    first = tr.invite_attendee(s, tenant_id=tenant_id, event_id=event.id, employee_id=employee_id)
    other = uuid.uuid4()
    s.add(
        people_models.Employee(
            id=other,
            tenant_id=tenant_id,
            party_id=uuid.uuid4(),
            employee_code="E002",
            date_of_joining=date(2025, 1, 1),
        )
    )
    s.flush()
    with pytest.raises(tr.TrainingError, match="full"):
        tr.invite_attendee(s, tenant_id=tenant_id, event_id=event.id, employee_id=other)
    tr.transition_attendee(s, tenant_id=tenant_id, attendee_id=first.id, to_status=tr.AttendeeStatus.CONFIRMED)
    done = tr.transition_attendee(
        s,
        tenant_id=tenant_id,
        attendee_id=first.id,
        to_status=tr.AttendeeStatus.ATTENDED,
        rating=Decimal("4.5"),
        certificate_number="CERT-1",
    )
    assert done.certificate_issued and done.rating == Decimal("4.5")

    cert = tr.Certification(
        tenant_id=tenant_id,
        employee_id=employee_id,
        name="Safety Cert",
        expiry_date=date(2026, 6, 1),
    )
    s.add(cert)
    s.flush()
    assert tr.expiring_certifications(s, tenant_id=tenant_id, before=date(2026, 7, 1)) == [cert]
