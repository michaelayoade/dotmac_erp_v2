"""B-2 held drafts: the published rules as pure functions, spec parsing, CLI refusals and the migration shape.

No database: the rules take plain data. The PostgreSQL behaviour is in test_held_drafts_pg.py.
"""

import json
import os
import stat
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from erp_v2.held_drafts import rules
from erp_v2.held_drafts.cli import main, parse_spec
from erp_v2.held_drafts.models import OUTCOME_POSTED, OUTCOME_QUEUED, OUTCOME_VOIDED
from erp_v2.held_drafts.rules import Draft, Evidence, LegacyRecord, Twin
from erp_v2.held_drafts.service import HeldDraftsRefused

BANK = uuid4()
OTHER = uuid4()
DAY = date(2026, 3, 10)
KEY = (BANK, DAY, Decimal("-5.000000"))


def fee(n: int, *, amount="5", day=DAY, account=BANK, extra=None, workflow=False) -> Draft:
    lines = {account: (Decimal(0), Decimal(amount)), OTHER: (Decimal(amount), Decimal(0))}
    lines.update(extra or {})
    return Draft(UUID(int=n), f"JE{n:05d}", day, Decimal(amount), "BANK_FEE", lines, workflow)


def legacy_fee(drafts) -> dict:
    return {d.journal_id: LegacyRecord("BANKING", "BANK_FEE", None) for d in drafts}


def evidence(drafts, *, s=0, p=0, **kwargs) -> Evidence:
    return Evidence(
        legacy=kwargs.pop("legacy", legacy_fee(drafts)),
        bank_gl_ids=frozenset({BANK}),
        statement={KEY: [uuid4() for _ in range(s)]} if s else {},
        posted_fees={KEY: [uuid4() for _ in range(p)]} if p else {},
        twins=kwargs.pop("twins", {}),
        **kwargs,
    )


def outcomes(decisions):
    out: dict[str, int] = {}
    for d in decisions:
        out[d.outcome] = out.get(d.outcome, 0) + 1
    return out


def test_bank_fee_keys_with_statement_at_least_posted_void_every_draft():
    drafts = [fee(n) for n in range(1, 6)]
    decisions = rules.decide(drafts, evidence(drafts, s=2, p=2))
    assert {d.outcome for d in decisions} == {OUTCOME_VOIDED}
    assert {d.reason_code for d in decisions} == {"duplicate_of_posted_fee"}
    assert all(d.rule_id == rules.RULE_BANK_FEE for d in decisions)


def test_bank_fee_statement_exceeding_posted_posts_the_difference_first_by_number():
    drafts = [fee(n) for n in range(1, 6)]
    decisions = rules.decide(drafts, evidence(drafts, s=4, p=2))
    posted = [d for d in decisions if d.outcome == OUTCOME_POSTED]
    assert outcomes(decisions) == {OUTCOME_POSTED: 2, OUTCOME_VOIDED: 3}
    assert [d.journal_id for d in posted] == [UUID(int=1), UUID(int=2)]  # lowest numbers post, the rest void


def test_bank_fee_postable_is_capped_by_the_number_of_drafts():
    drafts = [fee(1)]
    decisions = rules.decide(drafts, evidence(drafts, s=10, p=0))
    assert outcomes(decisions) == {OUTCOME_POSTED: 1}


def test_bank_fee_without_statement_evidence_queues():
    drafts = [fee(1), fee(2)]
    decisions = rules.decide(drafts, evidence(drafts, s=0, p=1))
    assert {(d.outcome, d.reason_code) for d in decisions} == {(OUTCOME_QUEUED, "no_statement_evidence")}


def test_bank_fee_with_more_posted_than_statement_voids_the_excess_and_raises_an_e17_finding():
    drafts = [fee(1), fee(2)]
    ev = evidence(drafts, s=1, p=4)
    decisions = rules.decide(drafts, ev)
    assert {(d.outcome, d.reason_code) for d in decisions} == {(OUTCOME_VOIDED, "duplicate_of_posted_fee_excess")}
    findings = rules.e17_findings(decisions, ev.posted_fees)
    assert len(findings) == 1  # one finding per key, however many drafts sit on it
    assert findings[0]["finding"] == "E17" and findings[0]["excess_posted_fee_journals"] == 3
    assert findings[0]["posted_fee_journals"] == 4 and findings[0]["statement_lines"] == 1
    assert findings[0]["drafts_voided"] == 2
    assert findings[0]["posted_journal_ids_sha256"] == rules.ids_sha256(ev.posted_fees[KEY])
    assert findings[0]["posted_journal_ids"] == sorted(str(i) for i in ev.posted_fees[KEY])  # Finance can act on them


def test_no_finding_when_posted_does_not_exceed_the_statement():
    drafts = [fee(1)]
    assert rules.e17_findings(rules.decide(drafts, evidence(drafts, s=2, p=2))) == []


def test_evidence_carries_a_count_and_digest_of_every_id_not_a_prefix():
    drafts = [fee(1)]
    ev = evidence(drafts, s=25, p=25)
    d = rules.decide(drafts, ev)[0]
    assert d.evidence["statement_lines"] == 25
    assert d.evidence["statement_line_ids_sha256"] == rules.ids_sha256(ev.statement[KEY])
    assert d.evidence["posted_journal_ids_sha256"] == rules.ids_sha256(ev.posted_fees[KEY])
    assert not any(isinstance(v, list) for v in d.evidence.values())


def test_a_key_on_a_shared_or_repointed_cash_gl_queues():
    draft = fee(1)
    ev = evidence([draft], s=3, p=0)
    unsafe = Evidence(
        legacy=ev.legacy,
        bank_gl_ids=frozenset(),
        statement=ev.statement,
        posted_fees=ev.posted_fees,
        twins={},
        unsafe_gl_ids=frozenset({BANK}),
    )
    d = rules.decide([draft], unsafe)[0]
    assert (d.outcome, d.reason_code) == (OUTCOME_QUEUED, "bank_gl_unresolved")


def test_bank_fee_that_is_not_a_single_bank_line_queues():
    no_bank = fee(1, account=uuid4())
    assert rules.decide([no_bank], evidence([no_bank], s=1, p=1))[0].reason_code == "not_single_bank_line"
    two_bank = fee(2)  # lines on BANK and OTHER; declare both as bank cash GLs
    ev = evidence([two_bank], s=1, p=1)
    both = Evidence(
        legacy=ev.legacy,
        bank_gl_ids=frozenset({BANK, OTHER}),
        statement=ev.statement,
        posted_fees=ev.posted_fees,
        twins={},
    )
    assert rules.decide([two_bank], both)[0].reason_code == "not_single_bank_line"


def test_same_amount_on_a_different_date_is_a_different_key():
    drafts = [fee(1, day=date(2026, 3, 11))]
    decisions = rules.decide(drafts, evidence(drafts, s=3, p=0))
    assert decisions[0].reason_code == "no_statement_evidence"  # the statement is on the 10th


def twin_ev(drafts, twin_lines, doc="D-1", kind="INVOICE"):
    legacy = {d.journal_id: LegacyRecord("AR", kind, doc) for d in drafts}
    return Evidence(
        legacy=legacy,
        bank_gl_ids=frozenset({BANK}),
        statement={},
        posted_fees={},
        twins={("AR", kind, doc): [Twin(uuid4(), twin_lines)]} if twin_lines is not None else {},
    )


def ar(n, kind="INVOICE", amount="100"):
    return Draft(
        UUID(int=n),
        f"AR{n:05d}",
        DAY,
        Decimal(amount),
        kind,
        {OTHER: (Decimal(amount), Decimal(0)), uuid4(): (Decimal(0), Decimal(amount))},
    )


def test_ar_twin_with_equal_per_account_amounts_voids():
    draft = ar(1)
    twin = dict(draft.lines)
    d = rules.decide([draft], twin_ev([draft], twin))[0]
    assert (d.outcome, d.reason_code, d.rule_id) == (OUTCOME_VOIDED, "already_posted_duplicate", rules.RULE_AR_TWIN)
    assert d.evidence["twin_journal_id"]


def test_ar_twin_equal_amounts_ignore_zero_lines_and_scale():
    draft = ar(1)
    twin = {**draft.lines, uuid4(): (Decimal("0.000000"), Decimal("0"))}
    assert rules.decide([draft], twin_ev([draft], twin))[0].outcome == OUTCOME_VOIDED


def test_ar_twin_with_different_amounts_queues():
    draft = ar(1)
    twin = {k: (d + 1, c + 1) for k, (d, c) in draft.lines.items()}
    d = rules.decide([draft], twin_ev([draft], twin))[0]
    assert (d.outcome, d.reason_code) == (OUTCOME_QUEUED, "twin_amount_differs")


def test_ar_twin_on_other_accounts_queues():
    draft = ar(1)
    twin = {uuid4(): (Decimal(100), Decimal(0)), uuid4(): (Decimal(0), Decimal(100))}
    assert rules.decide([draft], twin_ev([draft], twin))[0].reason_code == "twin_amount_differs"


def mirror_ev(draft, status, twin_lines=None, doc="D-1", kind="INVOICE"):
    ev = twin_ev(
        [draft], twin_lines if twin_lines is not None else {k: (c, d) for k, (d, c) in draft.lines.items()}, doc, kind
    )
    return Evidence(
        **{**{f: getattr(ev, f) for f in ev.__dataclass_fields__}, "document_status": {("AR", kind, doc): status}}
    )


@pytest.mark.parametrize("status", ["PAID", "POSTED", "PARTIALLY_PAID"])
def test_an_exact_mirror_of_a_posted_twin_on_a_live_document_voids_as_unapproved_reversal(status):
    draft = ar(1)
    d = rules.decide([draft], mirror_ev(draft, status))[0]
    assert (d.outcome, d.reason_code, d.rule_id) == (
        OUTCOME_VOIDED,
        "unapproved_reversal_of_live_document",
        rules.RULE_AR_REVERSAL,
    )
    assert (
        d.evidence["document_status"] == status and d.evidence["twin_journal_id"] and d.evidence["document_id"] == "D-1"
    )
    assert d.evidence["mirror_proof"]["mirror_sha256"] and d.evidence["mirror_proof"]["accounts"] == 2


def test_a_cleared_customer_payment_is_live_but_a_cleared_invoice_is_not():
    payment = ar(1, kind="CUSTOMER_PAYMENT")
    d = rules.decide([payment], mirror_ev(payment, "CLEARED", kind="CUSTOMER_PAYMENT"))[0]
    assert (d.outcome, d.reason_code) == (OUTCOME_VOIDED, "unapproved_reversal_of_live_document")
    invoice = ar(2)
    d = rules.decide([invoice], mirror_ev(invoice, "CLEARED"))[0]
    assert (d.outcome, d.reason_code) == (OUTCOME_QUEUED, "reversal_document_not_live")


def test_the_rules_text_says_p_includes_the_posts_this_tool_made():
    assert "INCLUDING" in rules.RULES_DEFINITION["rules"][rules.RULE_BANK_FEE]["P"]
    assert "excluding" not in rules.RULES_DEFINITION["rules"][rules.RULE_BANK_FEE]["P"]


@pytest.mark.parametrize("status", ["CANCELLED", "VOID", "DRAFT", None])
def test_a_mirror_of_a_document_that_is_not_live_queues(status):
    draft = ar(1)
    d = rules.decide([draft], mirror_ev(draft, status))[0]
    assert (d.outcome, d.reason_code) == (OUTCOME_QUEUED, "reversal_document_not_live")


def test_a_near_mirror_queues():
    draft = ar(1)
    off_by_one = {k: (c + (1 if c else 0), d) for k, (d, c) in draft.lines.items()}
    assert rules.decide([draft], mirror_ev(draft, "PAID", off_by_one))[0].reason_code == "twin_amount_differs"
    extra_account = {**{k: (c, d) for k, (d, c) in draft.lines.items()}, uuid4(): (Decimal(1), Decimal(0))}
    assert rules.decide([draft], mirror_ev(draft, "PAID", extra_account))[0].reason_code == "twin_amount_differs"


def test_the_equal_amount_rule_still_wins_over_the_mirror_rule():
    draft = ar(1)
    d = rules.decide([draft], mirror_ev(draft, "PAID", dict(draft.lines)))[0]
    assert d.reason_code == "already_posted_duplicate"


def test_is_mirror_is_exact_and_not_symmetric_noise():
    a, b = uuid4(), uuid4()
    assert rules.is_mirror(
        {a: (Decimal(5), Decimal(0)), b: (Decimal(0), Decimal(5))},
        {a: (Decimal(0), Decimal(5)), b: (Decimal(5), Decimal(0))},
    )
    assert not rules.is_mirror({}, {})
    same = {a: (Decimal(5), Decimal(0)), b: (Decimal(0), Decimal(5))}
    assert not rules.is_mirror(same, same)


def test_ar_without_a_posted_twin_or_document_queues():
    draft = ar(1)
    assert rules.decide([draft], twin_ev([draft], None))[0].reason_code == "twin_missing"
    nodoc = {draft.journal_id: LegacyRecord("AR", "INVOICE", " ")}
    ev = Evidence(legacy=nodoc, bank_gl_ids=frozenset(), statement={}, posted_fees={}, twins={})
    assert rules.decide([draft], ev)[0].reason_code == "no_document_reference"


def test_ar_customer_payment_follows_the_same_rule():
    draft = ar(1, kind="CUSTOMER_PAYMENT")
    assert rules.decide([draft], twin_ev([draft], dict(draft.lines), kind="CUSTOMER_PAYMENT"))[0].outcome == (
        OUTCOME_VOIDED
    )


def test_the_draft_is_never_its_own_twin():
    draft = ar(1)
    ev = twin_ev([draft], dict(draft.lines))
    ev.twins[("AR", "INVOICE", "D-1")] = [Twin(draft.journal_id, dict(draft.lines))]
    assert rules.decide([draft], ev)[0].reason_code == "twin_missing"


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (LegacyRecord("BANKING", "BANK_RECONCILIATION", None), "no_published_rule"),
        (LegacyRecord("EXPENSE", "EXPENSE_CLAIM", "x"), "no_published_rule"),
        (LegacyRecord("PAYROLL", "RUN", "x"), "no_published_rule"),
        (LegacyRecord("AP", "SUPPLIER_INVOICE", "x"), "no_published_rule"),
        (LegacyRecord(None, None, None), "no_published_rule"),
        (None, "no_legacy_record"),
    ],
)
def test_everything_else_goes_to_the_finance_queue(record, reason):
    draft = Draft(UUID(int=1), "X1", DAY, Decimal(1), None, {OTHER: (Decimal(1), Decimal(0))})
    legacy = {} if record is None else {draft.journal_id: record}
    ev = Evidence(legacy=legacy, bank_gl_ids=frozenset(), statement={}, posted_fees={}, twins={})
    d = rules.decide([draft], ev)[0]
    assert (d.outcome, d.rule_id, d.reason_code) == (OUTCOME_QUEUED, rules.RULE_QUEUE, reason)


def test_a_draft_with_a_workflow_in_progress_queues_and_a_kind_mismatch_queues():
    busy = fee(1, workflow=True)
    assert rules.decide([busy], evidence([busy], s=2, p=2))[0].reason_code == "workflow_in_progress"
    odd = fee(2)
    ev = evidence([odd], s=2, p=2, legacy={odd.journal_id: LegacyRecord("BANKING", "BANK_FEE", None)})
    wrong_kind = Draft(odd.journal_id, odd.number, odd.posting_date, odd.amount, "INVOICE", odd.lines)
    assert rules.decide([wrong_kind], ev)[0].reason_code == "legacy_record_mismatch"


def test_a_blocked_post_queues_and_the_remaining_drafts_still_void():
    drafts = [fee(n) for n in range(1, 4)]
    ev = evidence(drafts, s=2, p=0, post_blockers={UUID(int=1): "period_not_open"})
    got = {d.journal_id.int: (d.outcome, d.reason_code) for d in rules.decide(drafts, ev)}
    assert got[1] == (OUTCOME_QUEUED, "post_blocked")
    assert got[2] == (OUTCOME_POSTED, "statement_evidence_postable")
    assert got[3] == (OUTCOME_VOIDED, "duplicate_of_posted_fee")


def test_every_draft_gets_exactly_one_decision_and_the_partition_is_complete():
    drafts = [fee(n) for n in range(1, 8)] + [ar(20), ar(21)]
    legacy = {**legacy_fee(drafts[:7]), **{d.journal_id: LegacyRecord("AR", "INVOICE", "D-1") for d in drafts[7:]}}
    ev = Evidence(
        legacy=legacy,
        bank_gl_ids=frozenset({BANK}),
        statement={KEY: [uuid4()] * 3},
        posted_fees={KEY: [uuid4()]},
        twins={("AR", "INVOICE", "D-1"): [Twin(uuid4(), dict(drafts[7].lines))]},
    )
    decisions = rules.decide(drafts, ev)
    assert sorted(d.journal_id for d in decisions) == sorted(d.journal_id for d in drafts)
    assert len({d.journal_id for d in decisions}) == len(drafts)
    assert sum(outcomes(decisions).values()) == len(drafts)


def test_the_plan_sha_is_order_independent_and_changes_with_any_decision():
    drafts = [fee(n) for n in range(1, 5)]
    decisions = rules.decide(drafts, evidence(drafts, s=2, p=1))
    assert rules.plan_sha256(decisions) == rules.plan_sha256(list(reversed(decisions)))
    other = rules.decide(drafts, evidence(drafts, s=3, p=1))
    assert rules.plan_sha256(decisions) != rules.plan_sha256(other)
    assert rules.plan_sha256(decisions) != rules.plan_sha256(decisions, rules_sha256="0" * 64)


def test_rules_sha_is_a_stable_sha256():
    assert len(rules.RULES_SHA256) == 64 and int(rules.RULES_SHA256, 16) >= 0


# --- spec and CLI ---------------------------------------------------------------------------------------------


def spec(**changes):
    base = {
        "version": 1,
        "tenant_id": str(uuid4()),
        "fiscal_year_start": "2026-01-01",
        "fiscal_year_end": "2026-12-31",
        "rules_sha256": rules.RULES_SHA256,
        "evidence_ref": "Michael v1537 held drafts ruling",
        "requester_party_id": str(uuid4()),
        "approver_party_id": str(uuid4()),
        "filed_vat_months": ["2026-03"],
        "expected_held_total": 14271,
    }
    base.update(changes)
    return base


def test_spec_parses_and_binds_the_rules_version():
    scope, parties = parse_spec(json.dumps(spec()).encode())
    assert scope.fy_start == date(2026, 1, 1) and scope.filed_vat_months == {"2026-03"}
    assert scope.expected_held_total == 14271
    assert set(parties) == {"requester", "approver"}


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"rules_sha256": "0" * 64}, "rules_version_mismatch"),
        ({"tenant_id": "not-a-uuid"}, "invalid_tenant_id"),
        ({"fiscal_year_start": "2026-13-01"}, "invalid_fiscal_year"),
        ({"fiscal_year_end": "2025-12-31"}, "invalid_fiscal_year"),
        ({"evidence_ref": " "}, "evidence_ref_required"),
        ({"filed_vat_months": ["2026-3"]}, "invalid_filed_vat_months"),
        ({"requester_party_id": "x"}, "invalid_requester_party_id"),
        ({"version": 2}, "invalid_spec_schema"),
        ({"extra": 1}, "invalid_spec_schema"),
        ({"expected_held_total": "14271"}, "invalid_expected_held_total"),
        ({"expected_held_total": -1}, "invalid_expected_held_total"),
    ],
)
def test_spec_refusals(changes, code):
    with pytest.raises(HeldDraftsRefused) as exc:
        parse_spec(json.dumps(spec(**changes)).encode())
    assert exc.value.code == code


def test_spec_refuses_duplicate_keys():
    raw = json.dumps(spec()).encode()[:-1] + b', "version": 1}'
    with pytest.raises(HeldDraftsRefused) as exc:
        parse_spec(raw)
    assert exc.value.code == "duplicate_json_key"


def _spec_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec()))
    return path


def test_apply_without_the_explicit_apply_variable_refuses_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("ERP_HELD_DRAFTS_APPLY", raising=False)
    receipt = tmp_path / "r.json"
    code = main(["apply", "--spec", str(_spec_file(tmp_path)), "--receipt", str(receipt), "--operator-ref", "op"])
    assert code == 2 and not receipt.exists()


def test_an_existing_receipt_blocks_before_anything_else(tmp_path, monkeypatch):
    receipt = tmp_path / "r.json"
    receipt.write_text("keep")
    code = main(["plan", "--spec", str(_spec_file(tmp_path)), "--receipt", str(receipt), "--operator-ref", "op"])
    assert code == 2 and receipt.read_text() == "keep"


def test_plan_without_database_materials_refuses_with_a_private_receipt(tmp_path, monkeypatch):
    for name in ("MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    receipt = tmp_path / "r.json"
    code = main(["plan", "--spec", str(_spec_file(tmp_path)), "--receipt", str(receipt), "--operator-ref", "op"])
    assert code == 2
    assert stat.S_IMODE(os.stat(receipt).st_mode) == 0o600
    assert json.loads(receipt.read_text()) == {"ready": False, "refusal": "operator_database_materials_required"}


def test_serving_database_materials_are_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://u@db.invalid/x")
    monkeypatch.setenv("LEGACY_DATABASE_URL", "postgresql+psycopg://u@db.invalid/y")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u@db.invalid/z")
    receipt = tmp_path / "r.json"
    code = main(["plan", "--spec", str(_spec_file(tmp_path)), "--receipt", str(receipt), "--operator-ref", "op"])
    assert code == 2 and json.loads(receipt.read_text())["refusal"] == "serving_database_materials_forbidden"


def test_apply_needs_the_plan_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_HELD_DRAFTS_APPLY", "1")
    receipt = tmp_path / "r.json"
    code = main(["apply", "--spec", str(_spec_file(tmp_path)), "--receipt", str(receipt), "--operator-ref", "op"])
    assert code == 2 and json.loads(receipt.read_text())["refusal"] == "plan_binding_required"


def test_apply_refuses_a_plan_receipt_for_another_spec_or_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_HELD_DRAFTS_APPLY", "1")
    plan_receipt = tmp_path / "plan.json"
    plan_receipt.write_text(
        json.dumps(
            {
                "mode": "plan",
                "plan_sha256": "a" * 64,
                "spec_sha256": "b" * 64,
                "rules_sha256": rules.RULES_SHA256,
                "ready": True,
            }
        )
    )
    receipt = tmp_path / "r.json"
    code = main(
        [
            "apply", "--spec", str(_spec_file(tmp_path)), "--receipt", str(receipt), "--operator-ref", "op",
            "--plan-receipt", str(plan_receipt), "--approved-plan-sha256", "a" * 64,
        ]
    )  # fmt: skip
    assert code == 2 and json.loads(receipt.read_text())["refusal"] == "plan_binding_mismatch"


# --- migration shape ------------------------------------------------------------------------------------------


def test_migration_revision_and_chain():
    from erp_v2.migrate import EXPECTED_HEADS
    from erp_v2.migrations.versions import e2_0027_held_draft_decisions as migration

    assert migration.revision == "e2_0027_held_draft_decisions"
    assert migration.down_revision == "e2_0026_admission_reviews"
    from erp_v2.migrations.versions import e2_0028_migration_ledger as successor

    assert successor.down_revision == "e2_0027_held_draft_decisions"  # B-9 chains after this one
    assert "e2_0029_ar_opening_admissions" in EXPECTED_HEADS  # AR opening chains after e2_0028: one e2 head
    assert "e2_0027_held_draft_decisions" not in EXPECTED_HEADS
    assert "e2_0028_migration_ledger" not in EXPECTED_HEADS
    assert "e2_0026_admission_reviews" not in EXPECTED_HEADS


def test_no_estate_address_literals_in_the_package():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src" / "erp_v2" / "held_drafts"
    pattern = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    assert not [p.name for p in root.glob("*.py") if pattern.search(p.read_text())]
