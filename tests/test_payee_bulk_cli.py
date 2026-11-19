"""Bulk payee proposal CLI (B-11): sqlite for the logic; the service itself is covered by the B8 tests."""

from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel import audit_actions
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry
from dotmac_kernel.models import Base, Tenant
from dual_control_support import set_dual_control
from sqlalchemy.orm import Session

from erp_v2.governance.models import GovernancePolicy
from erp_v2.payees import bulk_cli, service
from erp_v2.payees.bank_codes import nuban_check_digit, valid_nuban
from erp_v2.payees.models import PayeeBankInstruction
from erp_v2.payees.routes import payee_bank_feature

PROPOSER = uuid4()
EMP = str(uuid4())


def nuban(serial9: str, bank: str = "058") -> str:
    return serial9 + str(nuban_check_digit(bank, serial9))


GOOD1, GOOD2, GOOD3, GOOD4 = (nuban(s) for s in ("012345678", "123456789", "234567890", "345678901"))
KNOWN = {
    ("supplier", "sup-1"): "Acme Supplies (system)",
    ("supplier", "sup-2"): "Beta Ltd (system)",
    ("employee", EMP): "E-001 Ada Test",
    ("statutory", "dest:PAYE"): "PAYE",
}


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(service, "resolve_payee", lambda db, tenant_id, kind, ref: KNOWN.get((kind, ref)))
    monkeypatch.setattr(
        audit_actions, "_active_registry", AuditActionRegistry.from_manifests([payee_bank_feature]), raising=False
    )
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, AuditEvent.__table__, PayeeBankInstruction.__table__, GovernancePolicy.__table__],
    )
    with Session(engine) as db:
        tenant = Tenant(id=uuid4(), slug="acme", name="Acme")
        db.add(tenant)
        db.flush()
        yield db, tenant.id
    engine.dispose()


def row(kind="supplier", ref="sup-1", number=GOOD1, **over):
    base = dict(
        kind=kind,
        ref=ref,
        bank_code="058",
        account_number=number,
        account_name="ACME SUPPLIES LTD",
        evidence_ref="signed-letter-2026-11-01",
    )
    if kind == "supplier":
        base["callback_ref"] = "call-2026-11-02"
    base.update(over)
    return base


def make_file(tenant, rows, **over):
    doc = dict(version=1, tenant_id=str(tenant), proposer_party_id=str(PROPOSER), file_ref="finance-batch-1", rows=rows)
    doc.update(over)
    raw = json.dumps(doc).encode()
    return bulk_cli.parse_file(raw, hashlib.sha256(raw).hexdigest())


def go(db, tenant, rows, *, apply=True, approve=False, operator="op-1"):
    return bulk_cli.run(db, make_file(tenant, rows), apply=apply, operator_ref=operator, approve=approve)


def instructions(db, status=None):
    q = sa.select(PayeeBankInstruction)
    if status:
        q = q.where(PayeeBankInstruction.status == status)
    return db.scalars(q).all()


def test_check_digit_rule():
    assert valid_nuban("058", GOOD1)
    assert not valid_nuban("058", GOOD1[:9] + str((int(GOOD1[9]) + 1) % 10))
    assert not valid_nuban("058", "12345")
    assert valid_nuban("50211", "1234567890")  # long institution codes are format-checked only


def test_plan_writes_nothing_and_apply_proposes_each_row(env):
    db, tenant = env
    rows = [row(), row("supplier", "sup-2", GOOD2), row("employee", EMP, GOOD3, account_name="ADA TEST")]
    plan = go(db, tenant, rows, apply=False)
    assert plan["counts"] == dict(requested=3, new=3, replace=0, unchanged=0, refused=0)
    assert instructions(db) == []
    done = go(db, tenant, rows)
    assert done["written"] == dict(proposed=3, replaced=0, approved=0)
    assert {i.status for i in instructions(db)} == {"proposed"}
    supplier = next(i for i in instructions(db) if i.payee_ref == "sup-1")
    assert supplier.evidence_ref == "signed-letter-2026-11-01 | callback:call-2026-11-02"
    assert supplier.bank_name == "Guaranty Trust Bank"


def test_unknown_payee_refused_and_nothing_written(env):
    db, tenant = env
    out = go(db, tenant, [row(), row("supplier", "nobody", GOOD2)])
    assert out["mode"] == "apply_refused" and out["written"] == dict(proposed=0, replaced=0, approved=0)
    assert out["refusals_by_reason"] == {"unknown_payee": 1}
    assert [r["outcome"] for r in out["rows"]] == ["new", "unknown_payee"]
    # the writer itself also refuses a not-fully-eligible file
    f = make_file(tenant, [row(), row("supplier", "nobody", GOOD2)])
    with pytest.raises(bulk_cli.BulkRefusal, match="file_not_fully_eligible"):
        bulk_cli.apply_rows(db, f, bulk_cli.classify(db, f), operator_ref="op-1", approve=False)
    assert instructions(db) == []


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"bank_code": "999"}, "unknown_bank_code"),
        ({"account_number": GOOD1[:9] + str((int(GOOD1[9]) + 1) % 10)}, "invalid_nuban"),
        ({"account_number": "12345"}, "invalid_nuban"),
        ({"callback_ref": None}, "callback_ref_required"),
    ],
)
def test_row_refusals(env, over, reason):
    db, tenant = env
    r = row()
    for k, v in over.items():
        if v is None:
            del r[k]
        else:
            r[k] = v
    assert go(db, tenant, [r], apply=False)["refusals_by_reason"] == {reason: 1}


def test_duplicate_open_proposal_replaces_the_earlier_one(env):
    db, tenant = env
    go(db, tenant, [row()])
    (first,) = instructions(db, "proposed")
    out = go(db, tenant, [row(number=GOOD2)])
    assert out["counts"]["replace"] == 1 and out["written"] == dict(proposed=1, replaced=1, approved=0)
    (second,) = instructions(db, "proposed")
    assert second.id != first.id and second.account_number == GOOD2
    assert db.get(PayeeBankInstruction, first.id).status == "rejected"
    assert "replaced by bulk file finance-batch-1" in db.get(PayeeBankInstruction, first.id).decision_note


def test_same_payee_twice_in_one_file_refused():
    with pytest.raises(bulk_cli.BulkRefusal, match="duplicate_payee_in_file"):
        make_file(uuid4(), [row(), row(number=GOOD2)])


def test_unchanged_row_is_a_noop_when_approved_details_match(env):
    db, tenant = env
    go(db, tenant, [row()], approve=True)
    out = go(db, tenant, [row()])
    assert out["counts"]["unchanged"] == 1 and out["written"]["proposed"] == 0


def test_statutory_path(env):
    db, tenant = env
    paye = row("statutory", "dest:PAYE", GOOD4, account_name="STATE IRS")
    out = go(db, tenant, [paye], approve=True)
    assert out["written"] == dict(proposed=1, replaced=0, approved=1)
    (inst,) = instructions(db, "approved")
    assert (inst.payee_kind, inst.payee_ref, inst.evidence_ref) == ("statutory", "dest:PAYE", paye["evidence_ref"])
    bad = go(db, tenant, [row("statutory", "dest:NOPE", GOOD4)], apply=False)
    assert bad["refusals_by_reason"] == {"statutory_ref_unresolved": 1}


def test_approve_only_with_policy_off_and_operator_ref(env):
    db, tenant = env
    out = go(db, tenant, [row()], approve=True)
    assert out["written"]["approved"] == 1 and instructions(db, "approved")[0].decided_by_party_id == PROPOSER
    with pytest.raises(bulk_cli.BulkRefusal, match="operator_ref_required"):
        go(db, tenant, [row("supplier", "sup-2", GOOD2)], approve=True, operator=None)


def test_policy_on_refuses_approve_but_still_allows_proposals(env):
    db, tenant = env
    set_dual_control(db, tenant, True)
    for apply in (False, True):
        with pytest.raises(bulk_cli.BulkRefusal, match="approve_refused_dual_control_on"):
            go(db, tenant, [row()], apply=apply, approve=True)
    assert instructions(db) == []
    assert go(db, tenant, [row()])["written"]["proposed"] == 1


def test_input_file_must_be_0600_regular_and_sha_bound(tmp_path):
    p = tmp_path / "f.json"
    p.write_text("{}")
    os.chmod(p, 0o644)
    with pytest.raises(bulk_cli.BulkRefusal, match="file_mode_not_0600"):
        bulk_cli.read_private_file(p)
    os.chmod(p, 0o600)
    assert bulk_cli.read_private_file(p) == b"{}"
    link = tmp_path / "l.json"
    link.symlink_to(p)
    with pytest.raises(bulk_cli.BulkRefusal, match="file_unreadable"):
        bulk_cli.read_private_file(link)
    with pytest.raises(bulk_cli.BulkRefusal, match="file_digest_mismatch"):
        bulk_cli.parse_file(b"{}", "0" * 64)


def test_receipt_is_0600_and_no_full_account_number_reaches_stdout_receipt_or_audit(env, tmp_path, monkeypatch, capsys):
    db, tenant = env
    doc = dict(
        version=1,
        tenant_id=str(tenant),
        proposer_party_id=str(PROPOSER),
        file_ref="finance-batch-1",
        rows=[row(), row("supplier", "sup-2", GOOD2), row("supplier", "nobody", GOOD3)],
    )
    raw = json.dumps(doc).encode()
    src = tmp_path / "in.json"
    src.write_bytes(raw)
    os.chmod(src, 0o600)
    sha = hashlib.sha256(raw).hexdigest()

    def fake_run(args):
        file = bulk_cli.parse_file(bulk_cli.read_private_file(args.file), args.file_sha256)
        return bulk_cli.run(db, file, apply=args.apply, operator_ref=args.operator_ref, approve=args.approve)

    monkeypatch.setattr(bulk_cli, "_run", fake_run)
    receipt = tmp_path / "r.json"
    code = bulk_cli.main(["--file", str(src), "--file-sha256", sha, "--receipt", str(receipt)])
    assert code == 3  # one row refused
    assert oct(receipt.stat().st_mode & 0o777) == "0o600"
    text = receipt.read_text() + capsys.readouterr().out
    body = json.loads(receipt.read_text())
    assert [r["account_last4"] for r in body["rows"]] == [GOOD1[-4:], GOOD2[-4:], GOOD3[-4:]]
    for number in (GOOD1, GOOD2, GOOD3):
        assert number not in text
    # an existing receipt is never overwritten
    assert bulk_cli.main(["--file", str(src), "--file-sha256", sha, "--receipt", str(receipt)]) == 2

    # apply the eligible subset: nothing PII in audit details either
    ok_doc = dict(doc, rows=doc["rows"][:2])
    ok_raw = json.dumps(ok_doc).encode()
    src.write_bytes(ok_raw)
    monkeypatch.setenv("ERP_IMPORT_APPROVED", "1")
    monkeypatch.setenv("ERP_IMPORT_APPLY_OK", "1")
    receipt2 = tmp_path / "r2.json"
    code = bulk_cli.main(
        [
            "--file", str(src), "--file-sha256", hashlib.sha256(ok_raw).hexdigest(), "--receipt", str(receipt2),
            "--apply", "--operator-ref", "op-1",
        ]
    )  # fmt: skip
    assert code == 0
    out = receipt2.read_text() + capsys.readouterr().out
    audit = " ".join(repr(e.details) for e in db.scalars(sa.select(AuditEvent)))
    for number in (GOOD1, GOOD2):
        assert number not in out and number not in audit


def test_apply_without_authorization_or_operator_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ERP_IMPORT_APPROVED", raising=False)
    receipt = tmp_path / "r.json"
    assert bulk_cli.main(["--file", "x", "--file-sha256", "0" * 64, "--receipt", str(receipt), "--apply"]) == 2
    assert not receipt.exists()
