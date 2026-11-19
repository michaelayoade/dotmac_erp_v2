# ruff: noqa: F811  (the B8 PG engine and audit fixtures are re-used by import)
"""Bulk payee CLI (B-11) on real PostgreSQL: the real statutory/employee/supplier resolvers and real B8 propose.

Runs only on the disposable hosted CI service (ERP_TEST_POSTGRES_URL); the engine fixture is the B8 one.
"""

import hashlib
import json
from uuid import uuid4

import sqlalchemy as sa
from dotmac_kernel.audit import AuditEvent
from dual_control_support import set_dual_control
from sqlalchemy.orm import Session
from test_payee_bank_pg import add_component, add_employee, audit_registry, engine, new_tenant  # noqa: F401

from erp_v2.payees import bulk_cli
from erp_v2.payees.bank_codes import nuban_check_digit, parse_bank_reference
from erp_v2.payees.models import PayeeBankInstruction

PROPOSER = uuid4()
BANKS_RAW = json.dumps(
    {"version": 1, "banks": [{"code": "058", "name": "Example Bank", "nuban_check": "check_digit"}]}
).encode()
BANKS = parse_bank_reference(BANKS_RAW, hashlib.sha256(BANKS_RAW).hexdigest())


def nuban(serial9):
    return serial9 + str(nuban_check_digit("058", serial9))


N1, N2, N3 = nuban("012345678"), nuban("123456789"), nuban("234567890")


def row(kind, ref, number, **over):
    base = dict(
        kind=kind,
        ref=ref,
        bank_code="058",
        account_number=number,
        account_name="NAME ON ACCOUNT",
        evidence_ref="signed-letter-1",
    )
    if kind == "supplier":
        base["callback_ref"] = "call-1"
    base.update(over)
    return base


def parsed(tenant, rows):
    raw = json.dumps(
        dict(version=1, tenant_id=str(tenant), proposer_party_id=str(PROPOSER), file_ref="batch-1", rows=rows)
    ).encode()
    return bulk_cli.parse_file(raw, hashlib.sha256(raw).hexdigest())


def go(engine, tenant, rows, **kw):
    kw.setdefault("operator_ref", "op-1")
    kw.setdefault("approve", False)
    with Session(engine) as db, db.begin():
        return bulk_cli.run(db, parsed(tenant, rows), BANKS, **kw)


def stored(engine, tenant, status=None):
    with Session(engine) as db:
        q = sa.select(PayeeBankInstruction).where(PayeeBankInstruction.tenant_id == tenant)
        if status:
            q = q.where(PayeeBankInstruction.status == status)
        return db.scalars(q).all()


def seed(engine, tenant, *, dual=False):
    with Session(engine) as db, db.begin():
        set_dual_control(db, tenant, dual)
        add_component(db, tenant, "dest:PAYE", name="PAYE", code="PAYE")
        employee = add_employee(db, tenant)
        return str(employee.id)


def test_real_resolvers_and_real_propose_for_all_three_kinds(engine):
    tenant = new_tenant(engine)
    emp = seed(engine, tenant)
    rows = [row("supplier", "sup-1", N1), row("employee", emp, N2), row("statutory", "dest:PAYE", N3)]
    plan = go(engine, tenant, rows, apply=False)
    assert plan["counts"] == dict(requested=3, new=3, replace=0, unchanged=0, refused=0)
    assert stored(engine, tenant) == []
    done = go(engine, tenant, rows, apply=True, approve=False)
    assert done["written"] == dict(proposed=3, replaced=0, approved=0)
    labels = {r.payee_kind: r.payee_label for r in stored(engine, tenant, "proposed")}
    assert labels == {"supplier": "System sup-1", "employee": "E001 Ada Test", "statutory": "PAYE"}
    sup = next(r for r in stored(engine, tenant) if r.payee_kind == "supplier")
    assert sup.evidence_ref == "signed-letter-1 | callback:call-1" and sup.bank_name == "Example Bank"
    with Session(engine) as db:
        events = db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == tenant)).all()
    assert events and all(n not in repr(e.details) for e in events for n in (N1, N2, N3))


def test_unresolved_statutory_and_unknown_payee_refuse_the_whole_file(engine):
    tenant = new_tenant(engine)
    seed(engine, tenant)
    rows = [row("supplier", "sup-1", N1), row("statutory", "dest:NOPE", N3), row("supplier", "ghost", N2)]
    out = go(engine, tenant, rows, apply=True, approve=False)
    assert out["mode"] == "apply_refused"
    assert out["refusals_by_reason"] == {"statutory_ref_unresolved": 1, "unknown_payee": 1}
    assert stored(engine, tenant) == []


def test_duplicate_open_proposal_is_replaced_on_postgres(engine):
    tenant = new_tenant(engine)
    seed(engine, tenant)
    go(engine, tenant, [row("supplier", "sup-1", N1)], apply=True, approve=False)
    out = go(engine, tenant, [row("supplier", "sup-1", N2)], apply=True, approve=False)
    assert out["written"] == dict(proposed=1, replaced=1, approved=0)
    (open_row,) = stored(engine, tenant, "proposed")
    assert open_row.account_number == N2 and len(stored(engine, tenant, "rejected")) == 1


def test_approve_with_policy_off_approves_and_the_db_checks_accept_it(engine):
    tenant = new_tenant(engine)
    emp = seed(engine, tenant, dual=False)
    rows = [row("supplier", "sup-1", N1), row("employee", emp, N2), row("statutory", "dest:PAYE", N3)]
    out = go(engine, tenant, rows, apply=True, approve=True)
    assert out["dual_control"] is False and out["written"] == dict(proposed=3, replaced=0, approved=3)
    approved = stored(engine, tenant, "approved")
    assert len(approved) == 3
    assert all(r.dual_control is False and r.decided_by_party_id == PROPOSER and r.approved_at for r in approved)


def test_approve_with_policy_on_is_refused_and_nothing_is_written(engine):
    tenant = new_tenant(engine)
    seed(engine, tenant, dual=True)
    for apply in (False, True):
        try:
            go(engine, tenant, [row("supplier", "sup-1", N1)], apply=apply, approve=True)
        except bulk_cli.BulkRefusal as exc:
            assert exc.reason == "approve_refused_dual_control_on"
        else:
            raise AssertionError("approve must be refused while dual control is ON")
    assert stored(engine, tenant) == []
    # proposals alone are still allowed; approval then stays with a second person
    assert go(engine, tenant, [row("supplier", "sup-1", N1)], apply=True, approve=False)["written"]["proposed"] == 1
    (proposal,) = stored(engine, tenant, "proposed")
    assert proposal.dual_control is True
