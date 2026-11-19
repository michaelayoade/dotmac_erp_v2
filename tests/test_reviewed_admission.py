# ruff: noqa: F811  (the imported `db` fixture is used by name)
"""Reviewed admission (B-1): the decision file, the waivable-check allowlist and the verifier hook (no PostgreSQL).

The verifier tests reuse the historical-evidence fixture (SQLite source and destination with the legacy column
names) and add the ``erp_admission_reviews`` table to the destination.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_payables_history import (  # noqa: F401  (fixtures and helpers of the historical-evidence suite)
    AP,
    DAY,
    EXP,
    INVOICE,
    STAMP,
    J,
    T,
    db,
    evidence,
    taxed,
)

from erp_v2.importer import admission_waivers as vocab
from erp_v2.importer.payables_history import HistoricalEvidenceError, WaiverGate
from erp_v2.legacy_ap import review_cli as cli
from erp_v2.legacy_ap.models import AdmissionReview

SNAPSHOT = "d" * 64  # the snapshot of the historical-evidence fixture manifest
INVOICE_2 = str(UUID(int=2000))

# What must stay mandatory: none of these verifier reasons may be reachable through any waiver name.
MANDATORY_REASONS = {
    "source_ap_control_credit",  # the GL tie: control credit == invoice total
    "invoice_control_other_postings",  # settled residual vs control
    "payment_gl_control_mismatch",
    "unsupported_currency",
    "command_snapshot",
    "command_fingerprint",
    "command_source_owner",
    "tenant_extent",
    "scope_tenant_required",
    "source_tenant_mismatch",
    "duplicate_admission",
    "journal_source_document_id",
    "journal_posting_stamp",
    "source_journal_totals",
    "source_journal_unbalanced",
    "source_journal_status",
    "source_journal_reversal",
    "invoice_line_tax_account",
    "source_withholding",
    "source_paid",
    "source_total",
}


# --- the allowlist ------------------------------------------------------------------------------------------------


def test_allowlist_excludes_the_gl_tie_and_every_integrity_check():
    covered = {reason for reasons in vocab.WAIVABLE_CHECKS.values() for reason in reasons}
    assert not covered & MANDATORY_REASONS
    assert not set(vocab.WAIVABLE_CHECKS) & set(vocab.NEVER_WAIVABLE)
    assert "gl_tie" not in vocab.WAIVABLE_CHECKS and "source_ap_control_credit" not in vocab.WAIVABLE_CHECKS


def test_the_vocabulary_is_the_closed_one_of_the_spec():
    assert set(vocab.WAIVABLE_CHECKS) == {
        "line_totals",
        "journal_owner",
        "inconsistent_status_pair",
        "source_amount_scale",
        "source_allocations",
        "legacy_gl_tax_treatment_unrecognised",
        "deferral_amount_mismatch",
        "legacy_journal_link_unposted",
    }
    assert set(vocab.DECISIONS) == {
        "admit_residual",
        "admit_with_waivers",
        "void",
        "ias8_post",
        "supplier_credit",
        "reopen",
    }
    assert vocab.MAX_WAIVERS_PER_INVOICE == 1  # D-M3: one named check per invoice


def test_gate_waives_only_a_named_check_and_only_for_the_reasons_that_name_covers():
    review = SimpleNamespace(
        review_id="r",
        decision="admit_with_waivers",
        waived=("line_totals",),
        evidence_sha="a" * 64,
        decision_file_sha="b" * 64,
    )
    gate = WaiverGate(review)
    gate.require(False, "invoice_line_totals", "line_totals")  # skipped
    assert gate.used == {"line_totals"}
    with pytest.raises(HistoricalEvidenceError, match="source_ap_control_credit"):
        gate.require(False, "source_ap_control_credit", "line_totals")  # the name does not cover this reason
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_totals"):
        WaiverGate().require(False, "invoice_line_totals", "line_totals")  # no decision: strict as before
    assert gate.facts()["review"]["waived"] == ["line_totals"]
    assert WaiverGate().facts() == {}  # no waiver: fingerprints of existing evidence are unchanged
    gate.finish()
    with pytest.raises(HistoricalEvidenceError, match="waiver_not_needed"):
        WaiverGate(review).finish()


# --- the decision file --------------------------------------------------------------------------------------------


def decision_row(**change):
    row = dict(
        legacy_invoice_id=INVOICE,
        decision="admit_with_waivers",
        waived_checks=["line_totals"],
        evidence_sha="e" * 64,
        decided_by="Finance Lead",
        decided_at="2026-11-20T10:00:00+01:00",
    )
    return row | change


def decision_file(rows=None, **change):
    base = dict(
        version=1,
        tenant_id=T,
        snapshot_sha256=SNAPSHOT,
        decision_ref="finance-pack-1",
        rows=[decision_row()] if rows is None else rows,
    )
    raw = json.dumps(base | change).encode()
    return raw, hashlib.sha256(raw).hexdigest()


def parse(raw, sha, snapshot=SNAPSHOT):
    return cli.parse_decision_file(raw, sha, snapshot)


def test_valid_decision_file_parses_and_is_sorted():
    other = str(UUID(int=1 << 100))
    raw, sha = decision_file(
        [decision_row(), decision_row(legacy_invoice_id=other, decision="admit_residual", waived_checks=[])]
    )
    parsed = parse(raw, sha)
    assert str(parsed.tenant_id) == T and parsed.sha256 == sha
    assert [str(r.legacy_invoice_id) for r in parsed.rows] == sorted([INVOICE, other])
    assert parsed.rows[0].decided_at.tzinfo is not None


def test_a_tampered_decision_file_is_refused():
    raw, sha = decision_file()
    with pytest.raises(cli.ReviewRefusal, match="decision_digest_mismatch"):
        parse(raw.replace(b"line_totals", b"journal_owner"), sha)  # one waiver swapped after Finance signed
    with pytest.raises(cli.ReviewRefusal, match="decision_digest_mismatch"):
        parse(raw + b" ", sha)
    with pytest.raises(cli.ReviewRefusal, match="decision_sha256_required"):
        parse(raw, "")
    with pytest.raises(cli.ReviewRefusal, match="decision_snapshot_mismatch"):
        parse(raw, sha, snapshot="c" * 64)


@pytest.mark.parametrize(
    "rows,reason",
    [
        ([decision_row(waived_checks=["source_ap_control_credit"])], "check_not_waivable"),
        ([decision_row(waived_checks=["gl_tie"])], "check_not_waivable"),
        ([decision_row(waived_checks=["residual_vs_control"])], "check_not_waivable"),
        ([decision_row(waived_checks=["unsupported_currency"])], "check_not_waivable"),
        ([decision_row(waived_checks=["line_totals", "journal_owner"])], "too_many_waivers"),
        ([decision_row(waived_checks=["line_totals", "line_totals"])], "duplicate_waived_check"),
        ([decision_row(waived_checks=[])], "waivers_do_not_match_decision"),
        ([decision_row(decision="admit_residual")], "waivers_do_not_match_decision"),
        ([decision_row(decision="void")], "waivers_do_not_match_decision"),
        ([decision_row(decision="mystery")], "unknown_decision"),
        ([decision_row(decision="ias8_post", waived_checks=[])], "invalid_row_schema"),  # needs the account
        ([decision_row(ias8_account_id=str(UUID(int=9)))], "invalid_row_schema"),  # only for ias8_post
        ([decision_row(evidence_sha="short")], "evidence_sha_required"),
        ([decision_row(evidence_sha=None)], "evidence_sha_required"),
        ([decision_row(decided_by=" padded ")], "decided_by_required"),
        ([decision_row(decided_at="2026-11-20T10:00:00")], "invalid_decided_at"),  # no offset
        ([decision_row(decided_at="yesterday")], "invalid_decided_at"),
        ([decision_row(legacy_invoice_id=str(UUID(int=0xABC)).upper())], "invalid_invoice_id"),
        ([decision_row(legacy_invoice_id="not-a-uuid")], "invalid_invoice_id"),
        ([decision_row(), decision_row()], "duplicate_invoice_id"),
        ([decision_row(extra=1)], "invalid_row_schema"),
        ([], "explicit_bounded_rows_required"),
        ([decision_row(legacy_invoice_id=str(UUID(int=n))) for n in range(1, 1002)], "explicit_bounded_rows_required"),
    ],
)
def test_decision_file_near_misses_refuse_the_whole_file(rows, reason):
    raw, sha = decision_file(rows)
    with pytest.raises(cli.ReviewRefusal, match=reason):
        parse(raw, sha)


def test_ias8_row_needs_its_account_and_no_waiver():
    row = decision_row(decision="ias8_post", waived_checks=[], ias8_account_id=str(UUID(int=31)))
    raw, sha = decision_file([row])
    assert parse(raw, sha).rows[0].ias8_account_id == UUID(int=31)
    raw, sha = decision_file([row | {"ias8_account_id": str(UUID(int=31)).upper()}])
    with pytest.raises(cli.ReviewRefusal, match="invalid_ias8_account_id"):
        parse(raw, sha)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"version": 2}, "invalid_decision_version"),
        ({"version": True}, "invalid_decision_version"),
        ({"tenant_id": "not-a-uuid"}, "invalid_tenant_id"),
        ({"decision_ref": " x"}, "decision_ref_required"),
        ({"extra": 1}, "invalid_decision_schema"),
    ],
)
def test_decision_file_header_near_misses(change, reason):
    raw, sha = decision_file(**change)
    with pytest.raises(cli.ReviewRefusal, match=reason):
        parse(raw, sha)


def test_duplicate_json_keys_are_refused():
    raw = b'{"version":1,"version":2}'
    with pytest.raises(cli.ReviewRefusal, match="duplicate_json_key"):
        parse(raw, hashlib.sha256(raw).hexdigest())


def test_apply_needs_both_explicit_authorizations_and_never_touches_a_database_without_them(
    monkeypatch, tmp_path, capsys
):
    for name in ("ERP_IMPORT_APPROVED", "ERP_IMPORT_APPLY_OK"):
        monkeypatch.delenv(name, raising=False)
    receipt = tmp_path / "r.json"
    code = cli.main(
        [
            "--decisions", str(tmp_path / "d.json"),
            "--decision-sha256", "a" * 64,
            "--snapshot-sha256", SNAPSHOT,
            "--receipt", str(receipt),
            "--operator-ref", "op",
            "--apply",
        ]
    )  # fmt: skip
    assert code == 2 and not receipt.exists()
    monkeypatch.setenv("ERP_IMPORT_APPROVED", "1")  # one of the two is not enough
    assert cli.main(["--decisions", "x", "--decision-sha256", "a" * 64, "--snapshot-sha256", SNAPSHOT,
                     "--receipt", str(receipt), "--apply"]) == 2  # fmt: skip
    assert not receipt.exists() and "refused" in capsys.readouterr().out


def test_plan_failure_receipt_is_private_and_opaque(monkeypatch, tmp_path):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://secret-user:secret-pw@127.0.0.1/none")
    receipt = tmp_path / "r.json"
    code = cli.main(
        [
            "--decisions", str(tmp_path / "missing.json"),
            "--decision-sha256", "a" * 64,
            "--snapshot-sha256", SNAPSHOT,
            "--receipt", str(receipt),
        ]
    )  # fmt: skip
    assert code == 2 and oct(receipt.stat().st_mode & 0o777) == "0o600"
    assert json.loads(receipt.read_text()) == dict(production_ready=False, refusal="admission_review_command_refused")
    assert "secret" not in receipt.read_text()


def test_receipt_holds_counts_and_digests_only():
    other = str(UUID(int=1 << 100))
    raw, sha = decision_file(
        [decision_row(), decision_row(legacy_invoice_id=other, decision="admit_residual", waived_checks=[])]
    )
    parsed = parse(raw, sha)
    counts = cli.Counter({"new": 1, "already_recorded": 1})
    receipt = cli.receipt_of(parsed, counts, mode="plan", dual=False)
    assert receipt["counts"] == dict(requested=2, new=1, already_recorded=1, refused=0)
    assert receipt["by_decision"] == {"admit_residual": 1, "admit_with_waivers": 1}
    text = json.dumps(receipt)
    assert INVOICE not in text and other not in text and "Finance Lead" not in text and T not in text


# --- the verifier hook (historical unpaid admission) --------------------------------------------------------------


def add_review(db, decision="admit_with_waivers", waived=("line_totals",), **change):
    table = AdmissionReview.__table__
    table.create(db[1], checkfirst=True)
    now = datetime.now(UTC)
    values = dict(
        id=uuid4(),
        tenant_id=UUID(T),
        legacy_invoice_id=UUID(INVOICE),
        decision=decision,
        waived_checks=list(waived),
        evidence_sha="e" * 64,
        decision_file_sha="f" * 64,
        snapshot_sha256=SNAPSHOT,
        decided_by="Finance Lead",
        decided_at=now,
        ias8_account_id=None,
        legacy_journal_id=UUID(J) if decision == "admit_with_waivers" else None,
        operator_ref="operator",
        dual_control=False,
        created_at=now,
        updated_at=now,
    )
    db[1].execute(table.insert().values(**(values | change)))


def scope():
    return SimpleNamespace(tenant_id=UUID(T))


def break_line_totals(db):
    """Source and destination line agree with each other but not with the header subtotal."""
    db[0].exec_driver_sql("PRAGMA query_only=0")
    for connection, table in (
        (db[0], db[4][True, "supplier_invoice_line"]),
        (db[1], db[4][False, "supplier_invoice_lines"]),
    ):
        connection.execute(table.update().values(line_amount="99"))


def break_journal_owner(db):
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].execute(db[4][True, "journal_entry"].update().values(source_module="AR"))


def break_status_pair(db):
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].execute(db[4][True, "supplier_invoice"].update().values(status="APPROVED"))  # posting_status stays POSTED


def break_control_credit(db):
    """The GL tie: the legacy journal's credit to the control account no longer equals the invoice total."""
    db[0].exec_driver_sql("PRAGMA query_only=0")
    for table in ("journal_entry_line", "posted_ledger_line"):
        t = db[4][True, table]
        db[0].execute(t.update().where(t.c.account_id == AP).values(credit_amount="90"))


def test_a_waived_check_admits_the_invoice_and_is_recorded_in_the_evidence(db):
    baseline = evidence(db).fingerprint()
    break_line_totals(db)
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_totals"):
        evidence(db)  # strict by default: no decision, no admission
    add_review(db, waived=("line_totals",))
    item = evidence(db)
    assert item.total_amount == Decimal("100") and item.fingerprint() != baseline
    # replay is deterministic and the command verifies against the same decision
    command = db[3].command(item)
    assert db[3].verify(scope=scope(), command=command, destination=db[2]) == item
    review = db[3].review(scope=scope(), invoice_id=UUID(INVOICE), destination=db[2])
    assert (
        review.waived == ("line_totals",) and review.decision_file_sha == "f" * 64 and review.evidence_sha == "e" * 64
    )


def test_the_waived_names_are_part_of_the_evidence_fingerprint(db):
    break_line_totals(db)
    add_review(db, waived=("line_totals",))
    fingerprint = evidence(db).fingerprint()
    db[1].execute(AdmissionReview.__table__.update().values(evidence_sha="a" * 64))
    assert evidence(db).fingerprint() != fingerprint  # a different Finance evidence is different evidence


def test_journal_owner_waiver_admits_only_the_ownership_check(db):
    break_journal_owner(db)
    with pytest.raises(HistoricalEvidenceError, match="source_journal_owner"):
        evidence(db)
    add_review(db, waived=("journal_owner",))
    assert evidence(db).total_amount == Decimal("100")


def test_status_pair_waiver_admits_and_reports_posted_to_the_owner(db):
    break_status_pair(db)
    with pytest.raises(HistoricalEvidenceError, match="source_status"):
        evidence(db)
    add_review(db, waived=("inconsistent_status_pair",))
    assert evidence(db).source_status == "POSTED"  # the POSTED legacy journal (still verified) proves it


def test_journal_link_unposted_waiver_uses_the_posted_journal_instant(db):
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].execute(
        db[4][True, "supplier_invoice"].update().values(status="APPROVED", posting_status="NOT_POSTED", posted_at=None)
    )
    with pytest.raises(HistoricalEvidenceError, match="source_status"):
        evidence(db)
    add_review(db, waived=("inconsistent_status_pair",))  # the wrong name for this shape: still refused
    with pytest.raises(HistoricalEvidenceError, match="source_status"):
        evidence(db)
    db[1].execute(AdmissionReview.__table__.delete())
    add_review(db, waived=("legacy_journal_link_unposted",))
    item = evidence(db)
    assert item.source_status == "POSTED" and item.original_posted_at.isoformat() == STAMP


def test_unposted_header_without_a_journal_link_is_not_a_link_waiver(db):
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].execute(
        db[4][True, "supplier_invoice"].update().values(status="APPROVED", posting_status="NOT_POSTED", posted_at=None)
    )
    db[0].execute(db[4][True, "supplier_invoice"].update().values(journal_entry_id=None))
    add_review(db, waived=("legacy_journal_link_unposted",))
    with pytest.raises(HistoricalEvidenceError):  # no journal: source_status (inconsistent pair) or the missing journal
        evidence(db)


PAYMENT, PAYMENT_JOURNAL = str(UUID(int=77)), str(UUID(int=78))


def with_payments(db, *payments):
    """A snapshot that identifies each allocation's payment: (status, journal id or None) per payment."""
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].exec_driver_sql("DROP TABLE IF EXISTS payment_allocation")
    db[0].exec_driver_sql("CREATE TABLE payment_allocation (invoice_id TEXT, payment_id TEXT)")
    db[0].exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS supplier_payment (payment_id TEXT, status TEXT, journal_entry_id TEXT)"
    )
    for number, (status, journal) in enumerate(payments):
        pay = str(UUID(int=77 + number * 10))
        db[0].exec_driver_sql("INSERT INTO payment_allocation VALUES (?, ?)", (INVOICE, pay))
        db[0].exec_driver_sql("INSERT INTO supplier_payment VALUES (?, ?, ?)", (pay, status, journal))


def payment_journal_line(db, account, debit):
    """A posted ledger line of the payment's journal with NO source_document_id (legacy leaves it empty on many
    journals): it can be found only through the payment's own journal link."""
    ledger = db[4][True, "posted_ledger_line"]
    row = dict(db[0].execute(sa.select(ledger).where(ledger.c.account_id == AP)).mappings().first())
    row.update(
        ledger_line_id=str(UUID(int=79)),
        journal_entry_id=PAYMENT_JOURNAL,
        source_document_id=None,
        account_id=account,
        debit_amount=debit,
        credit_amount="0",
    )
    db[0].execute(ledger.insert().values(**row))


def test_allocation_waiver_admits_when_the_payment_is_unposted(db):
    with_payments(db, ("DRAFT", None))
    with pytest.raises(HistoricalEvidenceError, match="source_allocations"):
        evidence(db)
    add_review(db, waived=("source_allocations",))
    assert evidence(db).allocation_count == 0


def test_allocation_waiver_admits_when_the_payments_journal_does_not_debit_the_control_account(db):
    with_payments(db, ("CLEARED", PAYMENT_JOURNAL))
    payment_journal_line(db, EXP, "40")
    add_review(db, waived=("source_allocations",))
    assert evidence(db).allocation_count == 0


def test_allocation_waiver_is_refused_when_the_payment_debited_the_control_account_without_a_source_document(db):
    """The payment-GL check stays mandatory and finds the ledger through the payment's journal link."""
    with_payments(db, ("CLEARED", PAYMENT_JOURNAL))
    payment_journal_line(db, AP, "40")  # source_document_id is NULL on this line
    add_review(db, waived=("source_allocations",))
    with pytest.raises(HistoricalEvidenceError, match="allocation_payment_gl_effect"):
        evidence(db)


@pytest.mark.parametrize(
    "payments",
    [
        [("CLEARED", None)],  # posts to the GL but no journal link
        [("CLEARED", PAYMENT_JOURNAL)],  # journal link but no ledger lines found: not proven
        [("DRAFT", None), ("CLEARED", PAYMENT_JOURNAL)],  # one unproven allocation is enough to refuse
    ],
)
def test_allocation_waiver_needs_positive_proof_for_every_payment(db, payments):
    with_payments(db, *payments)
    add_review(db, waived=("source_allocations",))
    with pytest.raises(HistoricalEvidenceError, match="allocation_payment_gl_unproven"):
        evidence(db)


def test_allocation_waiver_is_refused_when_the_payment_row_or_id_is_missing_or_the_snapshot_lacks_the_tables(db):
    # no payment_id column, no supplier_payment table
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].execute(db[4][True, "payment_allocation"].insert().values(invoice_id=INVOICE))
    add_review(db, waived=("source_allocations",))
    with pytest.raises(HistoricalEvidenceError, match="allocation_payment_gl_unproven"):
        evidence(db)
    # payment_id present but NULL, then pointing at a payment row that does not exist
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].exec_driver_sql("DROP TABLE payment_allocation")
    with_payments(db)
    db[0].exec_driver_sql("INSERT INTO payment_allocation (invoice_id) VALUES (?)", (INVOICE,))
    with pytest.raises(HistoricalEvidenceError, match="allocation_payment_gl_unproven"):
        evidence(db)
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].exec_driver_sql("DELETE FROM payment_allocation")
    db[0].exec_driver_sql("INSERT INTO payment_allocation VALUES (?, ?)", (INVOICE, PAYMENT))
    with pytest.raises(HistoricalEvidenceError, match="allocation_payment_gl_unproven"):
        evidence(db)


def test_one_legacy_journal_never_backs_two_invoices_and_the_check_is_never_waivable(db):
    """source_journal_shared: another legacy invoice references the same journal."""
    add_review(db, decision="admit_residual", waived=())
    db[0].exec_driver_sql("PRAGMA query_only=0")
    header = db[4][True, "supplier_invoice"]
    row = dict(db[0].execute(sa.select(header)).mappings().one())
    row.update(invoice_id=INVOICE_2, invoice_number="AP2")
    db[0].execute(header.insert().values(**row))
    with pytest.raises(HistoricalEvidenceError, match="source_journal_shared"):
        evidence(db)
    # a decision file cannot name it as a waiver: the name is not in the vocabulary, and a row that got into the
    # table anyway is refused as an invalid waiver
    assert "source_journal_shared" not in vocab.WAIVABLE_CHECKS
    db[1].execute(AdmissionReview.__table__.delete())
    add_review(db, waived=("source_journal_shared",))
    with pytest.raises(HistoricalEvidenceError, match="review_waivers_invalid"):
        evidence(db)


def test_a_receipt_that_already_names_the_journal_refuses(db):
    db[1].exec_driver_sql(
        "CREATE TABLE mod_payables.historical_invoice_receipts "
        "(invoice_id TEXT, source_journal_ref TEXT, destination_journal_ref TEXT)"
    )
    db[1].exec_driver_sql("INSERT INTO mod_payables.historical_invoice_receipts VALUES (?, ?, ?)", (INVOICE_2, J, J))
    with pytest.raises(HistoricalEvidenceError, match="source_journal_shared"):
        evidence(db)
    db[1].exec_driver_sql("DELETE FROM mod_payables.historical_invoice_receipts")
    db[1].exec_driver_sql(
        "INSERT INTO mod_payables.historical_invoice_receipts VALUES (?, ?, ?)", (INVOICE, J, J)
    )  # the invoice's own receipt (replay) is not a clash
    assert evidence(db).total_amount == Decimal("100")


def test_a_waiver_must_name_the_invoices_own_journal(db):
    break_line_totals(db)
    add_review(db, waived=("line_totals",), legacy_journal_id=UUID(int=999))
    with pytest.raises(HistoricalEvidenceError, match="review_journal_mismatch"):
        evidence(db)
    db[1].execute(AdmissionReview.__table__.delete())
    add_review(db, waived=("line_totals",), legacy_journal_id=None)
    with pytest.raises(HistoricalEvidenceError, match="review_journal_mismatch"):
        evidence(db)


def test_a_transform_fixed_failure_needs_no_waiver_and_is_kept_distinct_in_the_evidence():
    review = SimpleNamespace(
        review_id="r", decision="admit_with_waivers", waived=("line_totals",), evidence_sha="a" * 64,
        decision_file_sha="b" * 64,
    )  # fmt: skip
    gate = WaiverGate(None, {"source_amount_scale": "T-ROUND-2DP"})
    gate.require(False, "source_amount_scale", "source_amount_scale")  # fixed by the transform: no waiver, no refusal
    gate.finish()
    assert gate.facts() == {"transforms": {"source_amount_scale": "T-ROUND-2DP"}}
    both = WaiverGate(review, {"source_amount_scale": "T-ROUND-2DP"})
    both.require(False, "source_amount_scale", "source_amount_scale")
    both.require(False, "invoice_line_totals", "line_totals")
    both.finish()  # the transform did not use up or trip the waiver
    assert both.facts()["transforms"] == {"source_amount_scale": "T-ROUND-2DP"} and both.facts()["review"][
        "waived"
    ] == ["line_totals"]
    stale = WaiverGate(
        SimpleNamespace(**(vars(review) | {"waived": ("source_amount_scale",)})), {"source_amount_scale": "T-ROUND-2DP"}
    )
    stale.require(False, "source_amount_scale", "source_amount_scale")
    with pytest.raises(HistoricalEvidenceError, match="waiver_not_needed"):
        stale.finish()  # a waiver for what a transform already fixed is a stale waiver


def test_verifier_notes_carry_the_readable_status_and_the_correcting_journal_flag(db):
    from erp_v2.importer.payables_history import admission_notes

    taxed(db, "A3")
    add_review(db, waived=("deferral_amount_mismatch",))
    review = db[3].review(scope=scope(), invoice_id=UUID(INVOICE), destination=db[2])
    note = admission_notes(db[0], UUID(INVOICE), review)
    assert note["legacy_header_status"] == "POSTED/POSTED" and note["waived_checks"] == ["deferral_amount_mismatch"]
    assert note["requires_correcting_journal"] is True
    other = admission_notes(db[0], UUID(INVOICE), SimpleNamespace(**(vars(review) | {"waived": ("line_totals",)})))
    assert other["requires_correcting_journal"] is False
    assert admission_notes(db[0], UUID(INVOICE), None) == {}


def test_a3_deferral_mismatch_is_waived_by_its_specific_or_its_general_name(db):
    taxed(db, "A3")
    with pytest.raises(HistoricalEvidenceError, match="legacy_gl_tax_treatment_unrecognised"):
        evidence(db)
    add_review(db, waived=("legacy_gl_tax_treatment_unrecognised",))
    assert evidence(db).tax_amount == Decimal("15")


def test_a_non_waivable_check_still_refuses_even_with_a_waiver_on_the_invoice(db):
    break_line_totals(db)
    break_control_credit(db)  # the GL tie fails as well
    add_review(db, waived=("line_totals",))
    with pytest.raises(
        HistoricalEvidenceError, match=r"journal_line_credit|source_journal_totals|source_ap_control_credit"
    ):
        evidence(db)


@pytest.mark.parametrize("name", ["journal_owner", "line_totals", "source_allocations", "inconsistent_status_pair"])
def test_the_gl_tie_is_never_waivable_whatever_check_is_named(db, name):
    break_control_credit(db)
    add_review(db, waived=(name,))
    with pytest.raises(HistoricalEvidenceError):
        evidence(db)


def test_a_waiver_for_a_check_that_does_not_fail_is_refused(db):
    add_review(db, waived=("line_totals",))  # the invoice is fine: a stale or mistaken decision
    with pytest.raises(HistoricalEvidenceError, match="waiver_not_needed"):
        evidence(db)


def test_a_waiver_for_another_check_does_not_waive_this_one(db):
    break_line_totals(db)
    add_review(db, waived=("journal_owner",))
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_totals"):
        evidence(db)


@pytest.mark.parametrize("waived", [("source_ap_control_credit",), ("gl_tie",), ("line_totals", "journal_owner")])
def test_a_row_outside_the_allowlist_or_with_two_waivers_is_refused_even_if_it_got_into_the_table(db, waived):
    """The database CHECKs refuse these on PostgreSQL; the verifier does not trust the table either."""
    break_line_totals(db)
    add_review(db, waived=waived)
    with pytest.raises(HistoricalEvidenceError, match="review_waivers_invalid"):
        evidence(db)


def test_waivers_are_bound_to_the_snapshot_and_the_decision(db):
    break_line_totals(db)
    add_review(db, waived=("line_totals",), snapshot_sha256="c" * 64)
    with pytest.raises(HistoricalEvidenceError, match="review_snapshot_mismatch"):
        evidence(db)


@pytest.mark.parametrize("decision", ["void", "supplier_credit", "reopen", "ias8_post"])
def test_a_decision_other_than_admission_blocks_the_admission(db, decision):
    add_review(db, decision=decision, waived=(), ias8_account_id=uuid4() if decision == "ias8_post" else None)
    with pytest.raises(HistoricalEvidenceError, match="review_decision_not_admission"):
        evidence(db)


def test_admit_residual_changes_nothing(db):
    baseline = evidence(db).fingerprint()
    add_review(db, decision="admit_residual", waived=())
    assert evidence(db).fingerprint() == baseline


def test_another_tenants_or_invoices_decision_has_no_effect(db):
    break_line_totals(db)
    add_review(db, waived=("line_totals",), tenant_id=uuid4())
    add_review(db, waived=("line_totals",), legacy_invoice_id=UUID(INVOICE_2))
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_totals"):
        evidence(db)


def test_without_the_table_every_verifier_stays_strict(db):
    break_line_totals(db)
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_totals"):
        evidence(db)


def test_unpaid_admission_writes_one_reviewed_admission_audit_event_per_reviewed_invoice(db, monkeypatch):
    """The unpaid-admission CLI audits like settled admission and B1b: only invoices that honoured a decision."""
    import dotmac_kernel.audit as kernel_audit

    from erp_v2.importer import payables_history_cli as cli_unpaid

    written = []
    monkeypatch.setattr(kernel_audit, "write_audit_event", lambda session, **kw: written.append(kw))
    cli_unpaid._audit_reviewed(db[2], scope(), db[3], UUID(INVOICE), "cv1:" + "a" * 64)
    assert written == []  # no decision on file: nothing to audit
    break_line_totals(db)
    add_review(db, waived=("line_totals",))
    cli_unpaid._audit_reviewed(db[2], scope(), db[3], UUID(INVOICE), "cv1:" + "a" * 64)
    (event,) = written
    assert event["action"] == "erp.finance.supplier_invoice.legacy_unpaid_admitted"
    assert event["entity_id"] == INVOICE and event["tenant_id"] == UUID(T)
    details = event["details"]
    assert details["waived_checks"] == ["line_totals"] and details["legacy_header_status"] == "POSTED/POSTED"
    assert details["requires_correcting_journal"] is False and details["decision_file_sha"] == "f" * 64
