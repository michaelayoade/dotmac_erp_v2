"""Payee bank instruction lifecycle, two-person rule and PII handling (sqlite for the logic).

Real PostgreSQL (migration, privileges, RLS-capable shape, real audit rows) is covered by
tests/test_payee_bank_pg.py on the hosted CI service.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel import audit_actions
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry
from dotmac_kernel.models import Base, Tenant
from sqlalchemy.orm import Session

from erp_v2.payees import service
from erp_v2.payees.models import PayeeBankInstruction
from erp_v2.payees.routes import payee_bank_feature
from erp_v2.payees.service import PayeeBankError

NUMBER = "0123456789"
OTHER_NUMBER = "9876543210"
PREPARER, APPROVER, APPROVER2 = uuid4(), uuid4(), uuid4()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(
        audit_actions, "_active_registry", AuditActionRegistry.from_manifests([payee_bank_feature]), raising=False
    )
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Tenant.__table__, AuditEvent.__table__, PayeeBankInstruction.__table__])
    with Session(engine) as db:
        tenant = Tenant(id=uuid4(), slug="acme", name="Acme")
        db.add(tenant)
        db.flush()
        yield db, tenant.id
    engine.dispose()


def propose(db, tenant, *, ref="sup-1", number=NUMBER, by=PREPARER, kind="supplier", **over):
    args = dict(
        tenant_id=tenant,
        proposer_party_id=by,
        payee_kind=kind,
        payee_ref=ref,
        payee_label="Acme Supplies",
        bank_code="058",
        bank_name="Example Bank",
        account_number=number,
        account_name="ACME SUPPLIES LTD",
        evidence_ref="letter-2026-11-01",
    )
    args.update(over)
    return service.propose(db, **args)


def audit_rows(db):
    return db.scalars(sa.select(AuditEvent)).all()


def event(db, action):
    """The single audit event for an action (created_at ties inside one transaction, so never index by order)."""
    (found,) = [e for e in audit_rows(db) if e.action == action]
    return found


def test_propose_is_pending_masked_and_audited_without_the_number(env):
    db, tenant = env
    row = propose(db, tenant)
    assert (row.status, row.origin, row.proposed_by_party_id) == ("proposed", "manual", PREPARER)
    (view,) = service.list_instructions(db, tenant_id=tenant)
    assert view.account_masked == "******6789"
    assert NUMBER not in repr(view) and NUMBER not in repr(row) and NUMBER not in str(view)
    (event,) = audit_rows(db)
    assert event.action == service.AUDIT_PROPOSED and event.actor_id == str(PREPARER)
    assert NUMBER not in repr(event.details) and event.details["account_last4"] == "6789"


@pytest.mark.parametrize(
    "over,fragment",
    [
        ({"payee_kind": "customer"}, "payee_kind"),
        ({"bank_code": "AB"}, "bank_code"),
        ({"number": "12345"}, "10 digit"),
        ({"number": "01234567ab"}, "10 digit"),
        ({"evidence_ref": " "}, "evidence_ref"),
        ({"account_name": ""}, "account_name"),
        ({"bank_name": ""}, "bank_name"),
        ({"payee_ref": ""}, "payee_ref"),
    ],
)
def test_invalid_proposals_are_refused_without_echoing_the_number(env, over, fragment):
    db, tenant = env
    with pytest.raises(PayeeBankError) as err:
        propose(db, tenant, **over)
    assert err.value.code == "invalid_input" and fragment in str(err.value)
    assert NUMBER not in str(err.value) and "12345" not in str(err.value)
    assert db.scalar(sa.select(sa.func.count()).select_from(PayeeBankInstruction)) == 0


def test_proposer_cannot_approve_their_own_proposal(env):
    db, tenant = env
    row = propose(db, tenant)
    with pytest.raises(PayeeBankError) as err:
        service.approve(db, tenant_id=tenant, instruction_id=row.id, approver_party_id=PREPARER)
    assert err.value.code == "same_person"
    db.refresh(row)
    assert row.status == "proposed" and row.approved_at is None


def test_database_also_refuses_a_self_approved_row(env):
    db, tenant = env
    row = propose(db, tenant)
    with pytest.raises(sa.exc.IntegrityError):
        db.execute(
            sa.update(PayeeBankInstruction)
            .where(PayeeBankInstruction.id == row.id)
            .values(status="approved", approved_at=row.proposed_at, decided_by_party_id=PREPARER)
        )
    db.rollback()


def test_second_person_approves_and_audit_records_both(env):
    db, tenant = env
    row = propose(db, tenant)
    approved = service.approve(
        db, tenant_id=tenant, instruction_id=row.id, approver_party_id=APPROVER, note="called back"
    )
    assert approved.status == "approved" and approved.decided_by_party_id == APPROVER and approved.approved_at
    events = audit_rows(db)
    assert sorted(e.action for e in events) == sorted([service.AUDIT_PROPOSED, service.AUDIT_APPROVED])
    assert event(db, service.AUDIT_APPROVED).actor_id == str(APPROVER)
    assert event(db, service.AUDIT_PROPOSED).actor_id == str(PREPARER)
    assert all(NUMBER not in repr(e.details) for e in events)
    with pytest.raises(PayeeBankError) as again:
        service.approve(db, tenant_id=tenant, instruction_id=row.id, approver_party_id=APPROVER2)
    assert again.value.code == "wrong_state"


def test_approved_change_supersedes_only_after_second_approval_and_one_stays_active(env):
    db, tenant = env
    first = propose(db, tenant)
    service.approve(db, tenant_id=tenant, instruction_id=first.id, approver_party_id=APPROVER)
    second = propose(db, tenant, number=OTHER_NUMBER)
    # Until the second person approves, the old instruction is still the payment destination.
    assert (
        service.destination_for_payment(db, tenant_id=tenant, payee_kind="supplier", payee_ref="sup-1").instruction_id
        == first.id
    )
    service.approve(db, tenant_id=tenant, instruction_id=second.id, approver_party_id=APPROVER2)
    db.refresh(first)
    assert (first.status, first.superseded_by_id) == ("superseded", second.id) and first.superseded_at
    dest = service.destination_for_payment(db, tenant_id=tenant, payee_kind="supplier", payee_ref="sup-1")
    assert dest.instruction_id == second.id and dest.account_number == OTHER_NUMBER
    assert OTHER_NUMBER not in repr(dest) and OTHER_NUMBER not in str(dest)
    approved = db.scalars(sa.select(PayeeBankInstruction).where(PayeeBankInstruction.status == "approved")).all()
    assert [r.id for r in approved] == [second.id]


def test_one_open_proposal_per_payee_and_unchanged_details_refused(env):
    db, tenant = env
    row = propose(db, tenant)
    with pytest.raises(PayeeBankError) as open_err:
        propose(db, tenant, number=OTHER_NUMBER)
    assert open_err.value.code == "open_proposal_exists"
    service.approve(db, tenant_id=tenant, instruction_id=row.id, approver_party_id=APPROVER)
    with pytest.raises(PayeeBankError) as same:
        propose(db, tenant)
    assert same.value.code == "unchanged"


def test_reject_and_revoke_need_reasons_and_correct_states(env):
    db, tenant = env
    row = propose(db, tenant)
    with pytest.raises(PayeeBankError):
        service.reject(db, tenant_id=tenant, instruction_id=row.id, decider_party_id=APPROVER, reason=" ")
    db.flush()
    assert row.status == "proposed" and row.decided_by_party_id is None  # refusal mutated nothing
    with pytest.raises(PayeeBankError):
        service.approve(db, tenant_id=tenant, instruction_id=row.id, approver_party_id=APPROVER, note="n" * 501)
    db.flush()
    assert row.status == "proposed" and row.approved_at is None
    service.reject(db, tenant_id=tenant, instruction_id=row.id, decider_party_id=APPROVER, reason="evidence mismatch")
    assert row.status == "rejected"
    with pytest.raises(PayeeBankError) as err:
        service.approve(db, tenant_id=tenant, instruction_id=row.id, approver_party_id=APPROVER)
    assert err.value.code == "wrong_state"
    again = propose(db, tenant, number=OTHER_NUMBER)
    with pytest.raises(PayeeBankError) as not_approved:
        service.revoke(db, tenant_id=tenant, instruction_id=again.id, revoker_party_id=APPROVER, reason="x")
    assert not_approved.value.code == "wrong_state"
    service.approve(db, tenant_id=tenant, instruction_id=again.id, approver_party_id=APPROVER)
    service.revoke(db, tenant_id=tenant, instruction_id=again.id, revoker_party_id=APPROVER2, reason="account closed")
    assert again.status == "revoked" and again.revoked_by_party_id == APPROVER2
    with pytest.raises(PayeeBankError) as gone:
        service.destination_for_payment(db, tenant_id=tenant, payee_kind="supplier", payee_ref="sup-1")
    assert gone.value.code == "no_approved_instruction"


def test_pending_rejected_and_other_tenant_rows_are_never_a_payment_destination(env):
    db, tenant = env
    propose(db, tenant)
    with pytest.raises(PayeeBankError):
        service.destination_for_payment(db, tenant_id=tenant, payee_kind="supplier", payee_ref="sup-1")
    with pytest.raises(PayeeBankError):
        service.destination_for_payment(db, tenant_id=uuid4(), payee_kind="supplier", payee_ref="sup-1")


def _imported(db, tenant, *, bank_code, ref="emp-1"):
    row = PayeeBankInstruction(
        tenant_id=tenant,
        payee_kind="employee",
        payee_ref=ref,
        payee_label="E-001",
        bank_code=bank_code,
        bank_name="Example Bank",
        account_number=NUMBER,
        account_last4=NUMBER[-4:],
        account_name="A PERSON",
        status="proposed",
        origin="legacy_import",
        evidence_ref="legacy-import:employee.bank_account",
        proposed_by_party_id=None,
        proposed_at=sa.func.now(),
    )
    db.add(row)
    db.flush()
    return row


def test_imported_rows_need_individual_approval_and_incomplete_ones_cannot_be_approved(env):
    db, tenant = env
    incomplete = _imported(db, tenant, bank_code=None)
    with pytest.raises(PayeeBankError) as err:
        service.approve(db, tenant_id=tenant, instruction_id=incomplete.id, approver_party_id=APPROVER)
    assert err.value.code == "incomplete_instruction" and "bank_code" in str(err.value)
    assert NUMBER not in str(err.value) and incomplete.status == "proposed"
    complete = _imported(db, tenant, bank_code="058", ref="emp-2")
    service.approve(db, tenant_id=tenant, instruction_id=complete.id, approver_party_id=APPROVER)
    assert complete.status == "approved"


def test_unmasked_read_is_audited_needs_reason_and_logs_no_number(env):
    db, tenant = env
    row = propose(db, tenant)
    with pytest.raises(PayeeBankError):
        service.reveal_account_number(db, tenant_id=tenant, instruction_id=row.id, actor_party_id=APPROVER, reason="")
    assert (
        service.reveal_account_number(
            db, tenant_id=tenant, instruction_id=row.id, actor_party_id=APPROVER, reason="verify against letter"
        )
        == NUMBER
    )
    last = event(db, service.AUDIT_UNMASKED_READ)
    assert last.action == service.AUDIT_UNMASKED_READ and last.actor_id == str(APPROVER)
    assert NUMBER not in repr(last.details) and last.details["reason"] == "verify against letter"
    with pytest.raises(PayeeBankError) as missing:
        service.reveal_account_number(db, tenant_id=uuid4(), instruction_id=row.id, actor_party_id=APPROVER, reason="x")
    assert missing.value.code == "not_found"


def test_shared_account_across_payees_is_flagged_for_the_approver(env):
    db, tenant = env
    a = propose(db, tenant, ref="sup-1")
    b = propose(db, tenant, ref="sup-2")
    views = {v.payee_ref: v for v in service.list_instructions(db, tenant_id=tenant)}
    assert views["sup-1"].shared_account_payees == views["sup-2"].shared_account_payees == 1
    service.approve(db, tenant_id=tenant, instruction_id=a.id, approver_party_id=APPROVER)
    assert event(db, service.AUDIT_APPROVED).details["shared_account_payees"] == 1
    assert b.status == "proposed"


def test_no_account_number_anywhere_in_audit_trail_for_a_full_lifecycle(env):
    db, tenant = env
    row = propose(db, tenant)
    service.approve(db, tenant_id=tenant, instruction_id=row.id, approver_party_id=APPROVER)
    service.reveal_account_number(db, tenant_id=tenant, instruction_id=row.id, actor_party_id=APPROVER, reason="check")
    service.revoke(db, tenant_id=tenant, instruction_id=row.id, revoker_party_id=APPROVER, reason="closed")
    events = audit_rows(db)
    assert len(events) == 4
    assert not any(NUMBER in repr(e.details) or NUMBER in (e.entity_id or "") for e in events)
