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
from dotmac_kernel.models import Base, Party, PartyRoleGrant, PartyType, Role, Tenant
from dual_control_support import set_dual_control
from sqlalchemy.orm import Session

from erp_v2.governance.models import GovernancePolicy
from erp_v2.payees import bulk_cli, service
from erp_v2.payees.bank_codes import (
    BankReferenceError,
    nuban_check_digit,
    parse_bank_reference,
    valid_nuban,
)
from erp_v2.payees.models import PayeeBankInstruction
from erp_v2.payees.routes import payee_bank_feature

BANKS_RAW = json.dumps(
    {
        "version": 1,
        "banks": [
            {"code": "058", "name": "Example Bank", "nuban_check": "check_digit"},
            {"code": "50211", "name": "Example Fintech", "nuban_check": "format_only"},
            {"code": "044", "name": "Format Only Bank", "nuban_check": "format_only"},
        ],
    }
).encode()
BANKS_SHA = hashlib.sha256(BANKS_RAW).hexdigest()
BANKS = parse_bank_reference(BANKS_RAW, BANKS_SHA)
PROPOSER = uuid4()
NEW1 = dict(new=1, replace=0, change=0)
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
        tables=[
            Tenant.__table__,
            Party.__table__,
            Role.__table__,
            PartyRoleGrant.__table__,
            AuditEvent.__table__,
            PayeeBankInstruction.__table__,
            GovernancePolicy.__table__,
        ],
    )
    with Session(engine) as db:
        tenant = Tenant(id=uuid4(), slug="acme", name="Acme")
        db.add(tenant)
        db.flush()
        grant(db, tenant.id, PROPOSER, "finance_preparer", "finance_approver")
        yield db, tenant.id
    engine.dispose()


def grant(db, tenant_id, party_id, *slugs, active=True):
    db.add(Party(id=party_id, tenant_id=tenant_id, party_type=PartyType.person, display_name="P", is_active=active))
    db.flush()
    for slug in slugs:
        role = db.scalar(sa.select(Role).where(Role.tenant_id == tenant_id, Role.slug == slug))
        if role is None:
            role = Role(id=uuid4(), tenant_id=tenant_id, slug=slug, name=slug)
            db.add(role)
            db.flush()
        db.add(PartyRoleGrant(id=uuid4(), tenant_id=tenant_id, party_id=party_id, role_id=role.id))
    db.flush()


def row(kind="supplier", ref="sup-1", number=GOOD1, **over):
    base = dict(
        kind=kind,
        ref=ref,
        bank_code="058",
        account_number=number,
        account_name="ACME SUPPLIES LTD",
        evidence_ref="signed-letter-2026-11-01",
    )
    if kind in ("supplier", "statutory"):
        base["callback_ref"] = "call-2026-11-02"
    base.update(over)
    return base


def make_file(tenant, rows, **over):
    doc = dict(version=1, tenant_id=str(tenant), proposer_party_id=str(PROPOSER), file_ref="finance-batch-1", rows=rows)
    doc.update(over)
    raw = json.dumps(doc).encode()
    return bulk_cli.parse_file(raw, hashlib.sha256(raw).hexdigest())


def go(db, tenant, rows, *, apply=True, approve=False, operator="op-1", expect=None, file=None):
    return bulk_cli.run(
        db, file or make_file(tenant, rows), BANKS, apply=apply, operator_ref=operator, approve=approve, expect=expect
    )


def instructions(db, status=None):
    q = sa.select(PayeeBankInstruction)
    if status:
        q = q.where(PayeeBankInstruction.status == status)
    return db.scalars(q).all()


def test_check_digit_rule():
    assert valid_nuban("058", GOOD1, check_digit=True)
    assert not valid_nuban("058", GOOD1[:9] + str((int(GOOD1[9]) + 1) % 10), check_digit=True)
    assert not valid_nuban("058", "12345", check_digit=False)
    assert valid_nuban("50211", "1234567890", check_digit=False)


def test_check_digit_only_where_the_reference_marks_it_applicable():
    bad = GOOD1[:9] + str((int(GOOD1[9]) + 1) % 10)
    assert BANKS.valid_nuban("058", GOOD1) and not BANKS.valid_nuban("058", bad)
    assert BANKS.valid_nuban("044", bad)  # format_only: the same wrong digit passes
    assert BANKS.valid_nuban("50211", "1234567890")
    assert not BANKS.valid_nuban("044", "12345")


def test_bank_reference_is_required_sha_bound_and_strict():
    for raw, sha, reason in [
        (None, None, "bank_reference_required"),
        (BANKS_RAW, None, "bank_reference_required"),
        (BANKS_RAW, "0" * 64, "bank_reference_digest_mismatch"),
    ]:
        with pytest.raises(BankReferenceError, match=reason):
            parse_bank_reference(raw, sha)
    bad = json.dumps({"version": 1, "banks": [{"code": "50211", "name": "X", "nuban_check": "check_digit"}]}).encode()
    with pytest.raises(BankReferenceError, match="invalid_bank_reference"):
        parse_bank_reference(bad, hashlib.sha256(bad).hexdigest())
    with pytest.raises(bulk_cli.BulkRefusal, match="bank_reference_required"):
        bulk_cli.load_bank_reference(None, None)


def test_plan_writes_nothing_and_apply_proposes_each_row(env):
    db, tenant = env
    rows = [row(), row("supplier", "sup-2", GOOD2), row("employee", EMP, GOOD3, account_name="ADA TEST")]
    plan = go(db, tenant, rows, apply=False)
    assert plan["counts"] == dict(
        requested=3, new=3, replace=0, changes_to_approved=0, name_differs=0, unchanged=0, refused=0
    )
    assert instructions(db) == []
    done = go(db, tenant, rows)
    assert done["written"] == dict(proposed=3, replaced=0, approved=0, left_for_ui_approval=0)
    assert {i.status for i in instructions(db)} == {"proposed"}
    supplier = next(i for i in instructions(db) if i.payee_ref == "sup-1")
    assert supplier.evidence_ref == "signed-letter-2026-11-01 | callback:call-2026-11-02"
    assert supplier.bank_name == "Example Bank"
    assert done["bank_reference_sha256"] == BANKS_SHA


def test_unknown_payee_refused_and_nothing_written(env):
    db, tenant = env
    out = go(db, tenant, [row(), row("supplier", "nobody", GOOD2)])
    assert out["mode"] == "apply_refused" and out["written"] == dict(
        proposed=0, replaced=0, approved=0, left_for_ui_approval=0
    )
    assert out["refusals_by_reason"] == {"unknown_payee": 1}
    assert [r["outcome"] for r in out["rows"]] == ["new", "unknown_payee"]
    # the writer itself also refuses a not-fully-eligible file
    f = make_file(tenant, [row(), row("supplier", "nobody", GOOD2)])
    with pytest.raises(bulk_cli.BulkRefusal, match="file_not_fully_eligible"):
        bulk_cli.apply_rows(db, f, bulk_cli.classify(db, f, BANKS), banks=BANKS, operator_ref="op-1", approve=False)
    assert instructions(db) == []


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"bank_code": "999"}, "unknown_bank_code"),
        ({"bank_code": "044", "account_number": "12345"}, "invalid_nuban"),
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
    assert out["counts"]["replace"] == 1 and out["written"] == dict(
        proposed=1, replaced=1, approved=0, left_for_ui_approval=0
    )
    (second,) = instructions(db, "proposed")
    assert second.id != first.id and second.account_number == GOOD2
    assert db.get(PayeeBankInstruction, first.id).status == "rejected"
    assert "replaced by bulk file finance-batch-1" in db.get(PayeeBankInstruction, first.id).decision_note


def test_same_payee_twice_in_one_file_refused():
    with pytest.raises(bulk_cli.BulkRefusal, match="duplicate_payee_in_file"):
        make_file(uuid4(), [row(), row(number=GOOD2)])


def test_unchanged_row_is_a_noop_when_approved_details_match(env):
    db, tenant = env
    go(db, tenant, [row()], approve=True, expect=NEW1)
    out = go(db, tenant, [row()])
    assert out["counts"]["unchanged"] == 1 and out["written"]["proposed"] == 0


def test_statutory_path(env):
    db, tenant = env
    paye = row("statutory", "dest:PAYE", GOOD4, account_name="STATE IRS")
    out = go(db, tenant, [paye], approve=True, expect=NEW1)
    assert out["written"] == dict(proposed=1, replaced=0, approved=1, left_for_ui_approval=0)
    (inst,) = instructions(db, "approved")
    assert (inst.payee_kind, inst.payee_ref, inst.evidence_ref) == (
        "statutory",
        "dest:PAYE",
        paye["evidence_ref"] + " | callback:call-2026-11-02",
    )
    bad = go(db, tenant, [row("statutory", "dest:NOPE", GOOD4)], apply=False)
    assert bad["refusals_by_reason"] == {"statutory_ref_unresolved": 1}


def test_approve_only_with_policy_off_and_operator_ref(env):
    db, tenant = env
    out = go(db, tenant, [row()], approve=True, expect=NEW1)
    assert out["written"]["approved"] == 1 and instructions(db, "approved")[0].decided_by_party_id == PROPOSER
    with pytest.raises(bulk_cli.BulkRefusal, match="operator_ref_required"):
        go(db, tenant, [row("supplier", "sup-2", GOOD2)], approve=True, operator=None, expect=NEW1)


def test_policy_on_refuses_approve_but_still_allows_proposals(env):
    db, tenant = env
    set_dual_control(db, tenant, True)
    for apply in (False, True):
        with pytest.raises(bulk_cli.BulkRefusal, match="approve_refused_dual_control_on"):
            go(db, tenant, [row()], apply=apply, approve=True, expect=NEW1)
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
    ref = tmp_path / "banks.json"
    ref.write_bytes(BANKS_RAW)
    refargs = ["--bank-reference", str(ref), "--bank-reference-sha256", BANKS_SHA]

    def fake_run(args):
        file = bulk_cli.parse_file(bulk_cli.read_private_file(args.file), args.file_sha256)
        return bulk_cli.run(
            db,
            file,
            bulk_cli.load_bank_reference(args.bank_reference, args.bank_reference_sha256),
            apply=args.apply,
            operator_ref=args.operator_ref,
            approve=args.approve,
        )

    monkeypatch.setattr(bulk_cli, "_run", fake_run)
    receipt = tmp_path / "r.json"
    code = bulk_cli.main(["--file", str(src), "--file-sha256", sha, "--receipt", str(receipt), *refargs])
    assert code == 3  # one row refused
    assert oct(receipt.stat().st_mode & 0o777) == "0o600"
    text = receipt.read_text() + capsys.readouterr().out
    body = json.loads(receipt.read_text())
    assert [r["account_last4"] for r in body["rows"]] == [GOOD1[-4:], GOOD2[-4:], GOOD3[-4:]]
    for number in (GOOD1, GOOD2, GOOD3):
        assert number not in text
    # an existing receipt is never overwritten
    assert bulk_cli.main(["--file", str(src), "--file-sha256", sha, "--receipt", str(receipt), *refargs]) == 2

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
            "--apply", "--operator-ref", "op-1", *refargs,
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


def test_proposer_must_be_an_active_authorised_party_of_the_tenant(env):
    db, tenant = env
    other, inactive, preparer_only = uuid4(), uuid4(), uuid4()
    grant(db, tenant, preparer_only, "finance_preparer")
    grant(db, tenant, inactive, "finance_preparer", "finance_approver", active=False)
    foreign_tenant = Tenant(id=uuid4(), slug="other", name="Other")
    db.add(foreign_tenant)
    db.flush()
    grant(db, foreign_tenant.id, other, "finance_preparer", "finance_approver")

    def attempt(party, approve=False):
        raw = json.dumps(
            dict(version=1, tenant_id=str(tenant), proposer_party_id=str(party), file_ref="b", rows=[row()])
        ).encode()
        f = bulk_cli.parse_file(raw, hashlib.sha256(raw).hexdigest())
        return go(db, tenant, None, apply=False, approve=approve, file=f, expect=NEW1 if approve else None)

    for party in (uuid4(), other, inactive):  # unknown, another tenant's party, inactive
        with pytest.raises(bulk_cli.BulkRefusal, match="proposer_not_authorised"):
            attempt(party)
    assert attempt(preparer_only)["counts"]["new"] == 1  # propose permission is enough for plain proposals
    with pytest.raises(bulk_cli.BulkRefusal, match="proposer_not_authorised"):
        attempt(preparer_only, approve=True)  # --approve also needs the approve permission
    assert attempt(PROPOSER, approve=True)["counts"]["new"] == 1


def test_operator_ref_is_in_the_audit_details_of_every_bulk_event(env):
    db, tenant = env
    go(db, tenant, [row()], approve=True, operator="op-7", expect=NEW1)
    go(db, tenant, [row(number=GOOD2)], operator="op-7")
    events = db.scalars(sa.select(AuditEvent)).all()
    assert len(events) >= 3 and all(e.details.get("operator_ref") == "op-7" for e in events)


def approved_instruction(db, tenant):
    go(
        db,
        tenant,
        [row(), row("employee", EMP, GOOD3, account_name="ADA TEST")],
        approve=True,
        expect=dict(new=2, replace=0, change=0),
    )


def test_approve_skips_rows_that_change_an_approved_instruction(env):
    db, tenant = env
    approved_instruction(db, tenant)
    rows = [row(number=GOOD2), row("supplier", "sup-2", GOOD4)]  # sup-1: change; sup-2: onboarding
    out = go(db, tenant, rows, approve=True, expect=dict(new=1, replace=0, change=1))
    assert out["counts"]["changes_to_approved"] == 1 and out["counts"]["new"] == 1
    assert out["written"]["approved"] == 1 and out["written"]["left_for_ui_approval"] == 1
    by_ref = {i.payee_ref: i for i in instructions(db) if i.status in ("proposed", "approved")}
    assert by_ref["sup-2"].status == "approved"
    assert by_ref["sup-1"].status == "proposed" and by_ref["sup-1"].account_number == GOOD2
    # the old approved instruction stays the only active destination for sup-1
    assert any(
        i.payee_ref == "sup-1" and i.status == "approved" and i.account_number == GOOD1 for i in instructions(db)
    )


def test_missing_callback_refuses_for_supplier_statutory_and_changed_employee(env):
    db, tenant = env
    approved_instruction(db, tenant)
    for r in (
        row("supplier", "sup-2", GOOD4, callback_ref=None),
        row("statutory", "dest:PAYE", GOOD4, callback_ref=None),
        row("employee", EMP, GOOD4, account_name="ADA TEST", callback_ref=None),  # changes an approved one
    ):
        r.pop("callback_ref", None)
        assert go(db, tenant, [r], apply=False)["refusals_by_reason"] == {"callback_ref_required": 1}
    # a NEW employee (onboarding) needs no callback; a changed employee WITH one is a change
    KNOWN[("employee", "emp-new")] = "E-009 New"
    try:
        assert go(db, tenant, [row("employee", "emp-new", GOOD4)], apply=False)["counts"]["new"] == 1
    finally:
        del KNOWN[("employee", "emp-new")]
    changed = go(db, tenant, [row("employee", EMP, GOOD4, account_name="ADA TEST", callback_ref="c-1")], apply=False)
    assert changed["counts"]["changes_to_approved"] == 1


def test_expect_must_match_the_plan_counts_exactly(env):
    db, tenant = env
    approved_instruction(db, tenant)
    rows = [row(number=GOOD2), row("supplier", "sup-2", GOOD4)]
    ok = dict(new=1, replace=0, change=1)
    assert go(db, tenant, rows, apply=False, expect=ok)["expect_matches"] is True
    bad = go(db, tenant, rows, expect=dict(new=2, replace=0, change=0))
    assert bad["mode"] == "apply_refused_expect_mismatch" and bad["expect_matches"] is False
    assert [i for i in instructions(db) if i.status == "proposed"] == []  # nothing written
    done = go(db, tenant, rows, expect=ok)
    assert done["mode"] == "apply" and done["written"]["proposed"] == 2
    assert bulk_cli.parse_expect("new=1,replace=0,change=2") == dict(new=1, replace=0, change=2)
    for text in ("new=1", "new=1,replace=0,change=x", "new=1,replace=0,change=1,extra=2", "new=-1,replace=0,change=1"):
        with pytest.raises(bulk_cli.BulkRefusal, match="invalid_expect"):
            bulk_cli.parse_expect(text)


def test_name_differs_is_reported_and_proposed_not_skipped(env):
    db, tenant = env
    approved_instruction(db, tenant)
    out = go(
        db, tenant, [row(account_name="ACME SUPPLIES LIMITED")], approve=True, expect=dict(new=0, replace=0, change=1)
    )
    assert out["counts"]["name_differs"] == 1 and out["counts"]["changes_to_approved"] == 1
    assert [r["outcome"] for r in out["rows"]] == ["name_differs"]
    assert out["written"]["proposed"] == 1 and out["written"]["approved"] == 0  # never auto-approved
    (proposal,) = instructions(db, "proposed")
    assert proposal.account_name == "ACME SUPPLIES LIMITED"


def test_revised_nuban_scheme_pads_three_digit_codes_and_covers_six_digit_codes():
    serial = "012345678"
    # backward compatible: the padded scheme equals the original 12-weight scheme for three-digit codes
    weights = (3, 7, 3, 3, 7, 3, 3, 7, 3, 3, 7, 3)
    original = (10 - sum(int(d) * w for d, w in zip("058" + serial, weights, strict=True)) % 10) % 10
    assert nuban_check_digit("058", serial) == original
    six = "100004"
    digit = nuban_check_digit(six, serial)
    assert valid_nuban(six, serial + str(digit), check_digit=True)
    assert not valid_nuban(six, serial + str((digit + 1) % 10), check_digit=True)
    with pytest.raises(ValueError):
        nuban_check_digit("50211", serial)


def test_expect_is_required_with_approve_in_plan_and_apply_but_optional_without(env):
    db, tenant = env
    for apply in (False, True):
        with pytest.raises(bulk_cli.BulkRefusal, match="expect_required"):
            go(db, tenant, [row()], apply=apply, approve=True)
    assert instructions(db) == []
    assert go(db, tenant, [row()], approve=False)["written"]["proposed"] == 1  # propose-only: optional


def test_cli_refuses_approve_without_expect_before_any_database_work(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_IMPORT_APPROVED", "1")
    monkeypatch.setenv("ERP_IMPORT_APPLY_OK", "1")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://unused.invalid/none")
    src, banks = tmp_path / "in.json", tmp_path / "b.json"
    raw = json.dumps(
        dict(version=1, tenant_id=str(uuid4()), proposer_party_id=str(PROPOSER), file_ref="b", rows=[row()])
    ).encode()
    src.write_bytes(raw)
    os.chmod(src, 0o600)
    banks.write_bytes(BANKS_RAW)
    receipt = tmp_path / "r.json"
    code = bulk_cli.main(
        ["--file", str(src), "--file-sha256", hashlib.sha256(raw).hexdigest(), "--bank-reference", str(banks),
         "--bank-reference-sha256", BANKS_SHA, "--receipt", str(receipt), "--apply", "--approve",
         "--operator-ref", "op-1"]
    )  # fmt: skip
    assert code == 2 and json.loads(receipt.read_text())["refusal"] == "expect_required"
