"""Academy app-sync completion receiver (pilot flow 4, ERP destination).

Drives the real receiver through `deliver_authenticated` exactly as the
route does (minus transport HMAC, which is the route's job): first
delivery applies once with its certification consequence, the same key
redelivered answers already-applied, a changed body under the same key
conflicts, and an unmatched employee is rejected without a receipt so
the sender keeps it in its backlog.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from dotmac_app_sync import (
    AuthenticatedPeer,
    EnvelopeInvalid,
    SyncAcceptance,
    SyncEnvelope,
    deliver_authenticated,
    encode_envelope,
)
from dotmac_kernel import models as kernel_models
from dotmac_people import models as people_models
from sqlalchemy.orm import Session

from erp_v2.academy_sync.contracts import CAPABILITY_ID, OWNER_APPLICATION, SOURCE_APPLICATION, build_registry
from erp_v2.academy_sync.models import AcademyLearningCompletion, AcademySyncReceipt
from erp_v2.academy_sync.receiver import CompletionSyncReceiver
from erp_v2.hr.training import Certification


@pytest.fixture()
def session(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path}/main.db")
    with engine.begin() as conn:
        conn.execute(sa.text(f"ATTACH '{tmp_path}/people.db' AS mod_people"))
    kernel_models.Tenant.__table__.create(engine)
    people_models.Employee.__table__.create(engine)
    Certification.__table__.create(engine)
    AcademySyncReceipt.__table__.create(engine)
    AcademyLearningCompletion.__table__.create(engine)
    tenant_id = uuid.uuid4()
    employee_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(kernel_models.Tenant(id=tenant_id, slug="acme", name="Acme"))
        s.flush()
        s.add(
            people_models.Employee(
                id=employee_id,
                tenant_id=tenant_id,
                party_id=uuid.uuid4(),
                employee_code="E001",
                date_of_joining=date(2025, 1, 1),
            )
        )
        s.commit()
        yield s, tenant_id, employee_id


def _envelope(**payload_overrides) -> SyncEnvelope:
    payload = {
        "employee_ref": "E001",
        "academy_enrollment_ref": "enr-1",
        "academy_course_ref": "course-1",
        "course_title": "Safety Basics",
        "course_version": 2,
        "progress_pct": 100.0,
        "status": "completed",
        "occurred_at": "2026-11-08T10:00:00+00:00",
        "passed": True,
        "completed_on": "2026-11-08",
        "certificate_ref": "cert-1",
    }
    payload.update(payload_overrides)
    return SyncEnvelope(
        capability_id=CAPABILITY_ID,
        source_application=SOURCE_APPLICATION,
        source_event_id="completion-1",
        source_scope_kind="tenant",
        source_scope_ref="dotmac",
        subject_ref="person-1",
        occurred_at="2026-11-08T10:00:00+00:00",
        correlation_id="corr-1",
        payload=payload,
    )


def _deliver(db: Session, tenant_id, envelope: SyncEnvelope):
    return deliver_authenticated(
        encode_envelope(envelope),
        peer=AuthenticatedPeer(application=SOURCE_APPLICATION),
        expected_owner=OWNER_APPLICATION,
        registry=build_registry(),
        receiver=CompletionSyncReceiver(db, tenant_id=tenant_id),
    )


def test_first_delivery_applies_then_duplicate_and_conflict(session) -> None:
    db, tenant_id, employee_id = session
    first = _deliver(db, tenant_id, _envelope())
    assert first.acceptance is SyncAcceptance.ACCEPTED
    db.commit()

    completion = db.scalars(sa.select(AcademyLearningCompletion)).one()
    assert completion.employee_id == employee_id
    assert completion.progress_pct == Decimal("100.00")
    assert completion.completed_on == date(2026, 11, 8)
    assert first.destination_ref == f"academy_learning_completion:{completion.id}"
    certification = db.scalars(sa.select(Certification)).one()
    assert certification.credential_id == "cert-1"
    assert certification.name == "Safety Basics"
    assert certification.is_verified is True
    assert completion.certification_id == certification.id
    assert db.scalars(sa.select(AcademySyncReceipt)).one().destination_ref == first.destination_ref

    duplicate = _deliver(db, tenant_id, _envelope())
    db.commit()
    assert duplicate.acceptance is SyncAcceptance.ALREADY_APPLIED
    assert duplicate.destination_ref == first.destination_ref
    assert db.scalar(sa.select(sa.func.count()).select_from(AcademyLearningCompletion)) == 1
    assert db.scalar(sa.select(sa.func.count()).select_from(Certification)) == 1

    conflicted = _deliver(db, tenant_id, _envelope(course_title="Changed Title"))
    db.commit()
    assert conflicted.acceptance is SyncAcceptance.REJECTED
    assert conflicted.error_code == "fingerprint_conflict"
    assert db.scalar(sa.select(sa.func.count()).select_from(AcademyLearningCompletion)) == 1


def test_unmatched_employee_rejected_without_receipt(session) -> None:
    db, tenant_id, _employee_id = session
    receipt = _deliver(db, tenant_id, _envelope(employee_ref="NOPE"))
    db.commit()
    assert receipt.acceptance is SyncAcceptance.REJECTED
    assert receipt.error_code == "employee_unmatched"
    assert db.scalar(sa.select(sa.func.count()).select_from(AcademySyncReceipt)) == 0
    assert db.scalar(sa.select(sa.func.count()).select_from(AcademyLearningCompletion)) == 0


def test_payload_outside_destination_schema_fails_closed(session) -> None:
    db, tenant_id, _employee_id = session
    with pytest.raises(EnvelopeInvalid):
        _deliver(db, tenant_id, _envelope(status="in_progress"))
    with pytest.raises(EnvelopeInvalid):
        _deliver(db, tenant_id, _envelope(unexpected_key="x"))
