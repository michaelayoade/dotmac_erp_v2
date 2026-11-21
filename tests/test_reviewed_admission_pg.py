# ruff: noqa: F811  (imported fixtures are used by name)
"""Reviewed admission (B-1) against real PostgreSQL (hosted CI only): the table, the operator CLI and B1b.

Real payables/accounting migrations and commands. The B1b fixtures (imported-style APPROVED invoices, a legacy
snapshot file) are those of ``test_b1b_recognize_pg``.
"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import account_maintenance_support as accounts_gov
import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, PostedLedgerLine
from dotmac_payables import service as payables
from dotmac_payables.models import AccountingReceipt, SupplierInvoice
from dual_control_support import set_dual_control_engine
from sqlalchemy.orm import Session
from test_b1b_recognize_pg import (  # noqa: F401  (fixtures and helpers of the B1b suite)
    LIST_SHA,
    SNAP,
    Legacy,
    audit_registry,
    cli,
    counts,
    grant,
    imported,
    open_february,
    plan_then_apply,
    setup,
)
from test_posting_bridge_pg import soft_close

from erp_v2.legacy_ap import review_cli
from erp_v2.legacy_ap.models import AdmissionReview

engine = bridge_pg.engine
OPERATOR = "ops-1"


def add_3100(engine, env, *, code="3100") -> UUID:
    with Session(engine) as db:
        category = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "EQ")
        )
        if category is None:
            category = AccountCategory(
                tenant_id=env.tenant_id, code="EQ", name="Equity", account_class=AccountClass.EQUITY
            )
            db.add(category)
            db.flush()
        account = Account(
            tenant_id=env.tenant_id,
            category_id=category.id,
            code=code,
            name="Retained earnings",
            kind=AccountKind.POSTING,
            normal_balance=NormalBalance.CREDIT,
        )
        db.add(account)
        db.commit()
        return account.id


def row(invoice_id, decision="admit_with_waivers", waived=("source_allocations",), **change):
    base = dict(
        legacy_invoice_id=str(invoice_id),
        decision=decision,
        waived_checks=list(waived),
        evidence_sha="e" * 64,
        decided_by="Finance Lead",
        decided_at="2026-11-20T10:00:00+01:00",
    )
    return base | change


def record(engine, env, rows, *, operator=OPERATOR, apply=True, snapshot=SNAP):
    raw = json.dumps(
        dict(version=1, tenant_id=str(env.tenant_id), snapshot_sha256=snapshot, decision_ref="pack-1", rows=rows)
    ).encode()
    file = review_cli.parse_decision_file(raw, hashlib.sha256(raw).hexdigest(), snapshot)
    with Session(engine) as db, db.begin():
        return review_cli.run(db, file, apply=apply, operator_ref=operator)


def reviews(engine, env):
    with Session(engine) as db:
        return list(db.scalars(sa.select(AdmissionReview).where(AdmissionReview.tenant_id == env.tenant_id)))


# --- the operator CLI ---------------------------------------------------------------------------------------------


def test_apply_records_rows_and_one_audit_event_each_and_the_plan_writes_nothing(engine, setup):
    from dotmac_kernel.audit import AuditEvent

    from erp_v2.legacy_ap.audit import AUDIT_REVIEW_RECORDED

    env, _ = setup
    a, b = imported(engine, env, number="AP-1"), imported(engine, env, number="AP-2")
    rows = [row(a), row(b, decision="admit_residual", waived=())]
    plan = record(engine, env, rows, apply=False)
    assert plan["mode"] == "plan" and plan["counts"] == dict(requested=2, new=2, already_recorded=0, refused=0)
    assert reviews(engine, env) == []
    applied = record(engine, env, rows)
    assert applied["written"] == 2 and applied["dual_control"] is True  # the shared test tenant has the policy ON
    assert text_has_no_ids(applied, a, b)
    stored = {r.legacy_invoice_id: r for r in reviews(engine, env)}
    assert stored[a].waived_checks == ["source_allocations"] and stored[a].decision_file_sha
    assert stored[a].operator_ref == OPERATOR and stored[a].snapshot_sha256 == SNAP
    with Session(engine) as db:
        events = list(
            db.scalars(
                sa.select(AuditEvent).where(
                    AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == AUDIT_REVIEW_RECORDED
                )
            )
        )
    assert sorted(e.entity_id for e in events) == sorted([str(a), str(b)])
    detail = next(e.details for e in events if e.entity_id == str(a))
    assert (
        detail["waived_checks"] == ["source_allocations"] and detail["decision_file_sha"] == stored[a].decision_file_sha
    )
    assert detail["evidence_sha"] == "e" * 64 and detail["decided_by"] == "Finance Lead"


def text_has_no_ids(receipt, *ids):
    text = json.dumps(receipt)
    return not any(str(i) in text for i in ids) and "Finance Lead" not in text


def test_replay_is_a_no_op_and_a_different_decision_for_the_same_invoice_is_refused(engine, setup):
    env, _ = setup
    iid = imported(engine, env)
    record(engine, env, [row(iid)])
    again = record(engine, env, [row(iid)])
    assert again["written"] == 0 and again["counts"]["already_recorded"] == 1
    assert len(reviews(engine, env)) == 1
    other = record(engine, env, [row(iid, decision="void", waived=())], apply=False)
    assert other["refusals_by_reason"] == {"different_decision_recorded": 1}
    with pytest.raises(review_cli.ReviewRefusal, match="decision_file_not_fully_eligible"):
        record(engine, env, [row(iid, decision="void", waived=())])
    assert [r.decision for r in reviews(engine, env)] == ["admit_with_waivers"]


def test_unknown_invoice_refuses_the_whole_file(engine, setup):
    env, _ = setup
    iid = imported(engine, env)
    rows = [row(iid), row(UUID(int=777))]
    assert record(engine, env, rows, apply=False)["refusals_by_reason"] == {"invoice_not_found": 1}
    with pytest.raises(review_cli.ReviewRefusal, match="decision_file_not_fully_eligible"):
        record(engine, env, rows)
    assert reviews(engine, env) == []  # nothing partial


def test_dual_control_policy_is_default_off_and_stamped_and_enforced_when_on(engine, setup):
    env, _ = setup
    a, b, c = (imported(engine, env, number=f"AP-{n}") for n in range(3))
    set_dual_control_engine(engine, env.tenant_id, False)
    # OFF: one person may decide and apply
    out = record(engine, env, [row(a, decided_by="ops-1")], operator="ops-1")
    assert out["dual_control"] is False and reviews(engine, env)[0].dual_control is False
    set_dual_control_engine(engine, env.tenant_id, True)
    # ON: the operator applying must differ from the person who decided
    refused = record(engine, env, [row(b, decided_by="ops-1")], operator="ops-1", apply=False)
    assert refused["refusals_by_reason"] == {"dual_control_operator_is_decider": 1}
    with pytest.raises(review_cli.ReviewRefusal, match="decision_file_not_fully_eligible"):
        record(engine, env, [row(b, decided_by="ops-1")], operator="ops-1")
    ok = record(engine, env, [row(c, decided_by="Finance Lead")], operator="ops-1")
    assert ok["dual_control"] is True and {r.legacy_invoice_id: r.dual_control for r in reviews(engine, env)}[c] is True
    set_dual_control_engine(engine, env.tenant_id, False)  # leave the shared tenant policy as the default


def test_ias8_decision_needs_the_tenants_postable_3100(engine, setup):
    env, _ = setup
    iid = imported(engine, env)
    other = add_3100(engine, env, code="3200")
    bad = row(iid, decision="ias8_post", waived=(), ias8_account_id=str(other))
    assert record(engine, env, [bad], apply=False)["refusals_by_reason"] == {"ias8_account_not_3100": 1}
    missing = row(iid, decision="ias8_post", waived=(), ias8_account_id=str(uuid4()))
    assert record(engine, env, [missing], apply=False)["refusals_by_reason"] == {"ias8_account_not_3100": 1}
    good = row(iid, decision="ias8_post", waived=(), ias8_account_id=str(add_3100(engine, env)))
    assert record(engine, env, [good], apply=False)["counts"]["new"] == 1


# --- the table ----------------------------------------------------------------------------------------------------


def insert_sql(env, iid, **change):
    values = dict(
        id=str(uuid4()),
        t=str(env.tenant_id),
        i=str(iid),
        d="admit_with_waivers",
        w='["line_totals"]',
        e="e" * 64,
        f="f" * 64,
        s="d" * 64,
        by="Finance Lead",
        at=datetime.now(UTC),
        a=None,
        j=None,
        op="operator",
        dc=False,
    )
    values.update(change)
    return sa.text(
        "INSERT INTO erp_admission_reviews (id, tenant_id, legacy_invoice_id, decision, waived_checks, evidence_sha, "
        "decision_file_sha, snapshot_sha256, decided_by, decided_at, ias8_account_id, legacy_journal_id, operator_ref, "
        "dual_control) VALUES (:id, :t, :i, :d, CAST(:w AS jsonb), :e, :f, :s, :by, :at, :a, :j, :op, :dc)"
    ), values


def test_one_legacy_journal_cannot_back_two_invoices_in_the_table_or_the_plan(engine, setup):
    env, _ = setup
    a, b = imported(engine, env, number="AP-1"), imported(engine, env, number="AP-2")
    journal = str(uuid4())
    record(engine, env, [row(a, legacy_journal_id=journal)])
    # the plan refuses a second invoice naming the journal an earlier decision bound
    plan = record(engine, env, [row(b, legacy_journal_id=journal)], apply=False)
    assert plan["refusals_by_reason"] == {"journal_already_reviewed_for_another_invoice": 1}
    # and the database refuses it whatever the file said
    statement, values = insert_sql(env, b, j=journal)
    with engine.connect() as conn, pytest.raises(sa.exc.IntegrityError, match="uq_erp_admission_reviews_journal"):
        conn.execute(statement, values)


@pytest.mark.parametrize(
    "change,constraint",
    [
        (dict(w='["source_ap_control_credit"]'), "ck_erp_admission_reviews_vocabulary"),
        (dict(w='["gl_tie"]'), "ck_erp_admission_reviews_vocabulary"),
        (dict(w='["line_totals", "journal_owner"]'), "ck_erp_admission_reviews_one_waiver"),  # D-M3: one per invoice
        (dict(w="[]"), "ck_erp_admission_reviews_one_waiver"),  # admit_with_waivers names exactly one
        (dict(d="admit_residual"), "ck_erp_admission_reviews_one_waiver"),  # waivers only on that decision
        (dict(d="mystery", w="[]"), "ck_erp_admission_reviews_decision"),
        (dict(w='{"line_totals": 1}'), "ck_erp_admission_reviews_(waivers_array|one_waiver)"),
        (dict(e="short"), "ck_erp_admission_reviews_evidence_sha"),
        (dict(f="F" * 64), "ck_erp_admission_reviews_file_sha"),
        (dict(s="x"), "ck_erp_admission_reviews_snapshot_sha"),
        (dict(by="  "), "ck_erp_admission_reviews_decided_by"),
        (dict(d="ias8_post", w="[]"), "ck_erp_admission_reviews_ias8_account"),  # needs the account
        (dict(a=str(uuid4())), "ck_erp_admission_reviews_ias8_account"),  # only ias8_post carries it
        (dict(dc=True, op="Finance Lead"), "ck_erp_admission_reviews_sod"),
    ],
)
def test_the_database_refuses_rows_outside_the_vocabulary_and_the_rules(engine, setup, change, constraint):
    env, _ = setup
    iid = imported(engine, env)
    statement, values = insert_sql(env, iid, **change)
    with engine.connect() as conn, pytest.raises(sa.exc.IntegrityError, match=constraint):
        conn.execute(statement, values)


def test_rows_are_append_only_unique_per_invoice_and_runtime_can_only_select(engine, setup):
    env, _ = setup
    iid = imported(engine, env)
    statement, values = insert_sql(env, iid)
    with engine.begin() as conn:
        conn.execute(statement, values)
    with engine.connect() as conn:
        with pytest.raises(sa.exc.IntegrityError, match="uq_erp_admission_reviews_invoice"):
            conn.execute(*insert_sql(env, iid))
    for mutation in ("UPDATE erp_admission_reviews SET decided_by = 'x'", "DELETE FROM erp_admission_reviews"):
        with engine.connect() as conn, pytest.raises(sa.exc.DBAPIError, match="append-only"):
            conn.execute(sa.text(mutation + " WHERE tenant_id = :t"), {"t": str(env.tenant_id)})
    with engine.connect() as conn:
        grants = set(
            conn.scalars(
                sa.text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = 'erp_admission_reviews' AND grantee = 'app_user'"
                )
            )
        )
        forced = conn.scalar(
            sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'erp_admission_reviews'")
        )
    assert grants == {"SELECT"} and forced is True


def test_downgrade_refuses_while_decisions_exist(engine, setup):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from erp_v2.migrations.versions import e2_0026_admission_reviews as migration

    env, _ = setup
    record(engine, env, [row(imported(engine, env))])
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="admission review decisions exist"):
                    migration.downgrade()
        finally:
            transaction.rollback()
    with engine.connect() as c:
        assert c.scalar(sa.text("SELECT to_regclass('public.erp_admission_reviews')")) is not None
    assert migration.revision == "e2_0026_admission_reviews" and migration.down_revision == "e2_0024_statutory_payees"
    # with no rows it drops (inside a rolled-back transaction: the shared schema is untouched)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("TRUNCATE erp_admission_reviews"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert connection.scalar(sa.text("SELECT to_regclass('public.erp_admission_reviews')")) is None
        finally:
            transaction.rollback()


# --- B1b honours a waiver, and still refuses what is not waivable ------------------------------------------------


def zero_allocation(legacy, iid):
    """An allocation of an unposted legacy payment: the only kind a waiver may admit."""
    pay = str(uuid4())
    legacy.allocations.append(dict(invoice_id=str(iid), payment_id=pay))
    legacy.payments.append(dict(payment_id=pay, status="DRAFT", journal_entry_id=None))


def test_a_waived_check_admits_the_invoice_through_b1b_and_the_audit_names_the_waiver(engine, setup):
    from dotmac_kernel.audit import AuditEvent

    from erp_v2.legacy_ap.audit import AUDIT_RECOGNIZED_POSTED

    env, legacy = setup
    iid = imported(engine, env)
    zero_allocation(legacy, iid)
    legacy.build((iid,))
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"legacy_evidence:source_allocations": 1}
    record(engine, env, [row(iid, waived=("source_allocations",))])
    plan = legacy.run(engine, (iid,), apply=False)
    assert plan["counts"]["eligible"] == 1 and plan["waived"] == {"source_allocations": 1}
    applied = legacy.run(engine, (iid,), apply=True, digest=plan["plan_digest"])
    assert applied["applied"] == 1 and applied["posted_total"] == "215.00"
    with Session(engine) as db:
        event = db.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.tenant_id == env.tenant_id,
                AuditEvent.action == AUDIT_RECOGNIZED_POSTED,
                AuditEvent.entity_id == str(iid),
            )
        )
    assert (
        event.details["waived_checks"] == ["source_allocations"]
        and event.details["review_decision"] == "admit_with_waivers"
    )
    assert (
        event.details["review_id"] and event.details["decision_file_sha"] and event.details["evidence_sha"] == "e" * 64
    )


def test_a_non_waivable_check_still_refuses_through_b1b(engine, setup):
    env, legacy = setup
    iid = imported(engine, env)
    zero_allocation(legacy, iid)
    legacy.overrides[iid] = dict(amount_paid="1")  # source_paid: not in the allowlist
    legacy.build((iid,))
    record(engine, env, [row(iid, waived=("source_allocations",))])
    plan = legacy.run(engine, (iid,), apply=False)
    assert plan["refusals_by_reason"] == {"legacy_evidence:source_paid": 1} and plan["counts"]["eligible"] == 0
    before = counts(engine, env.tenant_id)
    with pytest.raises(cli.RecognizeRefusal, match="list_not_fully_eligible"):
        legacy.run(engine, (iid,), apply=True, digest=plan["plan_digest"])
    assert counts(engine, env.tenant_id) == before


def test_a_waiver_the_invoice_does_not_need_is_refused_and_another_decision_blocks_b1b(engine, setup):
    env, legacy = setup
    stale, voided = imported(engine, env, number="AP-1"), imported(engine, env, number="AP-2")
    legacy.build((stale, voided))
    record(engine, env, [row(stale, waived=("source_allocations",)), row(voided, decision="void", waived=())])
    refusals = legacy.run(engine, tuple(sorted((stale, voided), key=str)), apply=False)["refusals_by_reason"]
    assert refusals == {"legacy_evidence:waiver_not_needed": 1, "legacy_evidence:review_decision_not_admission": 1}


def test_allocation_waiver_never_hides_a_payment_posted_to_the_control_account(engine, setup):
    env, legacy = setup
    iid = imported(engine, env)
    pay, journal = str(uuid4()), str(uuid4())
    legacy.allocations.append(dict(invoice_id=str(iid), payment_id=pay))
    legacy.payments.append(dict(payment_id=pay, status="CLEARED", journal_entry_id=journal))
    legacy.ledger.append(  # no source_document_id: found only through the payment's journal link
        dict(
            ledger_line_id=str(uuid4()),
            journal_entry_id=journal,
            account_id=str(env.accounts["2000"]),
            debit_amount="10",
            credit_amount="0",
        )
    )
    legacy.build((iid,))
    record(engine, env, [row(iid, waived=("source_allocations",))])
    plan = legacy.run(engine, (iid,), apply=False)
    assert plan["refusals_by_reason"] == {"legacy_evidence:allocation_payment_gl_effect": 1}


def test_allocation_waiver_with_an_unposted_payment_admits_through_b1b(engine, setup):
    env, legacy = setup
    iid = imported(engine, env)
    pay = str(uuid4())
    legacy.allocations.append(dict(invoice_id=str(iid), payment_id=pay))
    legacy.payments.append(dict(payment_id=pay, status="DRAFT", journal_entry_id=None))
    legacy.build((iid,))
    record(engine, env, [row(iid, waived=("source_allocations",))])
    assert legacy.run(engine, (iid,), apply=False)["counts"]["eligible"] == 1


# --- IAS 8 posting mode (D-M5) ------------------------------------------------------------------------------------


def close_period(engine, env, period_id):
    from dotmac_accounting import service as accounting
    from dotmac_accounting.contracts import CloseCheck, PeriodCloseEvidence

    with Session(engine) as db:
        accounting.soft_close_period(
            db,
            scope=env.scope,
            period_id=period_id,
            evidence=PeriodCloseEvidence((CloseCheck("CHECK1", True, "evidence-1", "e" * 64),)),
            actor_ref="test",
            approval_reference="close-ias8",
            occurred_at=bridge_pg.NOW,
        )
        db.commit()


def fy2025_december(engine, env):
    """FY2025 (December, soft-closed) next to the open FY2026 January: the earlier-year shape."""
    from datetime import date

    from dotmac_accounting import service as accounting
    from dotmac_accounting.contracts import PeriodStatus
    from dotmac_accounting.models import FiscalPeriod, FiscalYear

    with Session(engine) as db:
        year = FiscalYear(
            tenant_id=env.tenant_id,
            code="FY25",
            name="FY 2025",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )
        db.add(year)
        db.flush()
        period = FiscalPeriod(
            tenant_id=env.tenant_id,
            fiscal_year_id=year.id,
            period_number=12,
            name="Dec",
            start_date=date(2025, 12, 1),
            end_date=date(2025, 12, 31),
            status=PeriodStatus.FUTURE,
        )
        db.add(period)
        db.flush()
        accounting.open_period(db, scope=env.scope, period_id=period.id, actor_ref="test", occurred_at=bridge_pg.NOW)
        db.commit()
        period_id = period.id
    close_period(engine, env, period_id)


def ias8_world(engine, env, legacy):
    iid = imported(engine, env, number="AP-IAS8", period_date="2025-12-10")
    legacy.build((iid,))
    fy2025_december(engine, env)
    return iid, add_3100(engine, env)


def decide_ias8(engine, env, iid, account):
    return record(engine, env, [row(iid, decision="ias8_post", waived=(), ias8_account_id=str(account))])


def test_ias8_mode_posts_in_the_first_open_period_with_the_expense_side_on_3100(engine, setup):
    from dotmac_kernel.audit import AuditEvent

    from erp_v2.legacy_ap.audit import AUDIT_RECOGNIZED_POSTED

    env, legacy = setup
    iid, account = ias8_world(engine, env, legacy)
    # without the decision the closed period still refuses, exactly as before
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"posting_period_not_open": 1}
    decide_ias8(engine, env, iid, account)
    plan = legacy.run(engine, (iid,), apply=False)
    assert plan["counts"] == dict(requested=1, eligible=1, already_done=0, refused=0) and plan["ias8_prior_period"] == 1
    applied = legacy.run(engine, (iid,), apply=True, digest=plan["plan_digest"])
    assert applied["applied"] == 1 and applied["posted_total"] == "215.00"
    review = reviews(engine, env)[0]
    with Session(engine) as db:
        invoice = db.get(SupplierInvoice, iid)
        assert invoice.invoice_date.isoformat() == "2025-12-10"  # the document is never re-dated
        receipt = db.scalar(sa.select(AccountingReceipt).where(AccountingReceipt.document_id == iid))
        journal = db.get(JournalEntry, UUID(receipt.accounting_reference))
        assert journal.posting_date.isoformat() == "2026-01-01" and journal.entry_date.isoformat() == "2026-01-01"
        assert journal.source_owner == "payables" and journal.source_document_id == str(iid)
        # the review id and decision are the evidence carried on the journal
        assert str(review.id) in journal.description and review.decision_file_sha[:16] in journal.description
        assert str(review.id) in journal.approval_reference
        lines = {
            db.get(Account, r.account_id).code: (r.debit, r.credit)
            for r in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal.id))
        }
        # expense side on 3100; the input-tax debit (V1 account) and the 2000 control credit are untouched
        assert lines == {"3100": (200, 0), "1455": (15, 0), "2000": (0, 215)}
        assert "5000" not in lines
        # the owner's receipt is against ITS consequence (the standard one), unchanged
        consequence = payables.build_invoice_accounting_consequence(db, tenant_id=env.tenant_id, invoice_id=iid)
        assert receipt.consequence_fingerprint == consequence.fingerprint
        assert receipt.accounting_evidence_fingerprint == journal.request_fingerprint
        event = db.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.tenant_id == env.tenant_id,
                AuditEvent.action == AUDIT_RECOGNIZED_POSTED,
                AuditEvent.entity_id == str(iid),
            )
        )
    assert event.details["review_id"] == str(review.id) and event.details["ias8_posting_date"] == "2026-01-01"
    assert event.details["legacy_header_status"] == "APPROVED/NOT_POSTED"
    # replay is a no-op
    again = legacy.run(engine, (iid,), apply=False)
    assert again["counts"] == dict(requested=1, eligible=0, already_done=1, refused=0)
    assert legacy.run(engine, (iid,), apply=True, digest=again["plan_digest"])["applied"] == 0


def test_a_soft_closed_month_of_the_same_fiscal_year_never_posts_to_3100(engine, setup):
    """January soft-closed, February open: both FY2026. IAS 8 is for an earlier year only."""
    env, legacy = setup
    open_february(engine, env)
    iid = imported(engine, env, number="AP-SAMEYEAR", period_date="2026-01-10")
    legacy.build((iid,))
    soft_close(engine, env)
    decide_ias8(engine, env, iid, add_3100(engine, env))
    plan = legacy.run(engine, (iid,), apply=False)
    assert plan["refusals_by_reason"] == {"ias8_same_fiscal_year": 1} and plan["counts"]["eligible"] == 0
    before = counts(engine, env.tenant_id)
    with pytest.raises(cli.RecognizeRefusal, match="list_not_fully_eligible"):
        legacy.run(engine, (iid,), apply=True, digest=plan["plan_digest"])
    assert counts(engine, env.tenant_id) == before


def test_ias8_refuses_when_the_period_is_open(engine, setup):
    env, legacy = setup
    # the invoice's period is OPEN: this is not a prior-period item, the normal path applies
    iid = imported(engine, env, number="AP-OPEN")
    open_february(engine, env)
    legacy.build((iid,))
    decide_ias8(engine, env, iid, add_3100(engine, env))
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"ias8_period_open_use_normal_path": 1}


def test_ias8_refuses_without_a_later_open_period(engine, setup):
    env, legacy = setup
    iid = imported(engine, env, number="AP-NOLATER")
    legacy.build((iid,))
    soft_close(engine, env)  # January closed, no other period exists
    decide_ias8(engine, env, iid, add_3100(engine, env))
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"ias8_no_later_open_period": 1}


def test_ias8_account_that_stopped_being_3100_postable_is_refused(engine, setup):
    env, legacy = setup
    iid, account = ias8_world(engine, env, legacy)
    decide_ias8(engine, env, iid, account)
    with Session(engine) as db:
        accounts_gov.retire(db, account)
        db.commit()
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"ias8_account_not_postable": 1}
    with Session(engine) as db:
        accounts_gov.activate(db, account)
        db.commit()
    assert legacy.run(engine, (iid,), apply=False)["counts"]["eligible"] == 1
