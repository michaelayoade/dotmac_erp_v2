"""B1b recognize-and-post against real PostgreSQL (hosted CI only).

Real payables/accounting migrations and commands; the legacy snapshot is a read-only SQLite
file carrying the legacy column names the verifier reads. Imported-style invoices are made
APPROVED by direct update, exactly as the importer does (no liability event, no obligation,
no receipt, no journal).
"""

import json
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.models import Account, JournalEntry, PostedLedgerLine
from dotmac_payables import service as payables
from dotmac_payables.contracts import InvoiceStatus
from dotmac_payables.models import (
    AccountingReceipt,
    HistoricalInvoiceReceipt,
    InvoiceRecognition,
    LiabilityEvent,
    PaymentObligation,
    SupplierInvoice,
    SupplierInvoiceLine,
)
from sqlalchemy.orm import Session
from test_b1_supplier_invoice_post_pg import NOW, invoice, world
from test_posting_bridge_pg import soft_close

from erp_v2.importer.payables_history import READ_FIELDS
from erp_v2.importer.snapshot import SnapshotManifest
from erp_v2.legacy_ap import recognize_cli as cli

engine = bridge_pg.engine
LIST_SHA = "c" * 64
SNAP = "d" * 64
REVISION = "e" * 40
STAMP = "2025-01-01T00:00:00+00:00"
MODELS = (InvoiceRecognition, LiabilityEvent, PaymentObligation, AccountingReceipt, JournalEntry)
SOURCE_TABLES = {
    "supplier_invoice": READ_FIELDS["supplier_invoice"],
    "supplier_invoice_line": READ_FIELDS["supplier_invoice_line"],
    "payment_allocation": READ_FIELDS["payment_allocation"],
    "journal_entry": READ_FIELDS["journal_entry"],
    "posted_ledger_line": READ_FIELDS["posted_ledger_line"],
}
BOOLEANS = {"is_prepayment", "is_reversal"}
INTEGERS = {"line_number"}


def counts(engine, tenant_id) -> dict[str, int]:
    with Session(engine) as db:
        return {
            m.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == tenant_id))
            for m in MODELS
        }


def imported(engine, env, *, number="AP-1", status_value="APPROVED"):
    """An imported-style invoice: APPROVED by direct update, no event/obligation/receipt/journal."""
    iid = invoice(engine, env, number=number, submit=True)
    with Session(engine) as db:
        db.execute(sa.update(SupplierInvoice).where(SupplierInvoice.id == iid).values(status=status_value))
        db.commit()
    return iid


class Legacy:
    """A read-only legacy snapshot file derived from the destination invoices."""

    def __init__(self, tmp_path, env, db_engine):
        self.path = tmp_path / "source.db"
        self.env = env
        self.engine = db_engine
        self.overrides: dict[UUID, dict] = {}
        self.ledger: list[dict] = []
        self.allocations: list[dict] = []
        with db_engine.connect() as dest:
            self.destination_name = dest.scalar(sa.text("SELECT current_database()"))

    def manifest(self):
        return SnapshotManifest(SNAP, "source.db", self.destination_name, REVISION, (str(self.env.tenant_id),))

    def build(self, ids):
        source = sa.create_engine(f"sqlite:///{self.path}")
        with source.begin() as conn, Session(self.engine) as db:
            tables = {}
            for name, fields in SOURCE_TABLES.items():
                tables[name] = sa.Table(
                    name,
                    sa.MetaData(),
                    *(
                        sa.Column(f, sa.Boolean if f in BOOLEANS else sa.Integer if f in INTEGERS else sa.Text)
                        for f in fields
                    ),
                )
                tables[name].create(conn)
            headers, lines = [], []
            for iid in ids:
                inv = db.get(SupplierInvoice, iid)
                header = dict.fromkeys(READ_FIELDS["supplier_invoice"])
                header.update(
                    organization_id=str(self.env.tenant_id),
                    invoice_id=str(iid),
                    supplier_id=inv.supplier_ref,
                    invoice_number=inv.number,
                    supplier_invoice_number=inv.supplier_document_number,
                    invoice_type="STANDARD",
                    status="APPROVED",
                    posting_status="NOT_POSTED",
                    invoice_date=inv.invoice_date.isoformat(),
                    received_date=inv.received_date.isoformat(),
                    currency_code=inv.currency_code,
                    exchange_rate=str(inv.exchange_rate),
                    subtotal=str(inv.subtotal),
                    tax_amount=str(inv.tax_amount),
                    total_amount=str(inv.total_amount),
                    amount_paid="0",
                    functional_currency_amount=str(inv.total_amount),
                    ap_control_account_id=inv.liability_account_ref,
                    withholding_tax_amount="0",
                    prepayment_applied="0",
                    is_prepayment=False,
                    stamp_duty_amount="0",
                    created_at=STAMP,
                )
                header.update(self.overrides.get(iid, {}))
                headers.append(header)
                for line in db.scalars(sa.select(SupplierInvoiceLine).where(SupplierInvoiceLine.invoice_id == iid)):
                    row = dict.fromkeys(READ_FIELDS["supplier_invoice_line"])
                    row.update(
                        line_id=str(line.id),
                        invoice_id=str(iid),
                        line_number=line.line_number,
                        description=line.description,
                        quantity=str(line.quantity),
                        unit_price=str(line.unit_price),
                        line_amount=str(line.line_amount),
                        tax_amount=str(line.tax_amount),
                        expense_account_id=line.posting_account_ref,
                        created_at=STAMP,
                    )
                    lines.append(row)
            for name, rows in (
                ("supplier_invoice", headers),
                ("supplier_invoice_line", lines),
                ("payment_allocation", self.allocations),
                ("posted_ledger_line", self.ledger),
            ):
                if rows:
                    conn.execute(tables[name].insert(), rows)
        source.dispose()

    def run(self, engine, ids, *, apply, digest=None, actor=None, now=NOW, readonly=None):
        source = sa.create_engine(f"sqlite:///{self.path}")
        try:
            with source.connect() as src:
                src.exec_driver_sql("PRAGMA query_only=1")
                with Session(engine) as db, db.begin():
                    if readonly if readonly is not None else not apply:
                        db.execute(sa.text("SET TRANSACTION READ ONLY"))
                    return cli.run_cohort(
                        db,
                        src,
                        self.manifest(),
                        tenant=self.env.tenant_id,
                        ids=ids,
                        list_sha256=LIST_SHA,
                        actor_party_id=actor or self.env.party_ids[1],
                        apply=apply,
                        reviewed_digest=digest,
                        bindings=dict(list_sha256=LIST_SHA),
                        now=now,
                    )
        finally:
            source.dispose()


@pytest.fixture
def setup(engine, tmp_path):
    env = world(engine)
    return env, Legacy(tmp_path, env, engine)


def plan_then_apply(engine, legacy, ids):
    plan = legacy.run(engine, ids, apply=False)
    return plan, legacy.run(engine, ids, apply=True, digest=plan["plan_digest"])


def test_plan_is_read_only_and_reports_counts_and_totals_without_ids(engine, setup):
    env, legacy = setup
    ids = tuple(sorted([imported(engine, env, number="AP-1"), imported(engine, env, number="AP-2")], key=str))
    legacy.build(ids)
    before = counts(engine, env.tenant_id)
    plan = legacy.run(engine, ids, apply=False)  # runs inside SET TRANSACTION READ ONLY
    assert plan["mode"] == "plan" and plan["counts"] == dict(requested=2, eligible=2, already_done=0, refused=0)
    assert plan["totals"]["eligible"] == dict(subtotal="400.00", tax="30.00", total="430.00")
    assert counts(engine, env.tenant_id) == before == {m.__tablename__: 0 for m in MODELS}
    text = json.dumps(plan)
    assert not any(str(i) in text for i in ids)


def test_apply_recognizes_and_posts_dr_expense_cr_2000(engine, setup):
    env, legacy = setup
    ids = tuple(sorted([imported(engine, env, number="AP-1"), imported(engine, env, number="AP-2")], key=str))
    legacy.build(ids)
    plan, applied = plan_then_apply(engine, legacy, ids)
    assert applied["applied"] == 2 and applied["posted_total"] == "430.00"
    assert applied["plan_digest"] == plan["plan_digest"]
    assert counts(engine, env.tenant_id) == dict(
        invoice_recognitions=2, liability_events=2, payment_obligations=2, accounting_receipts=2, journal_entries=2
    )
    with Session(engine) as db:
        for iid in ids:
            inv = db.get(SupplierInvoice, iid)
            assert inv.status == InvoiceStatus.APPROVED and inv.total_amount == Decimal("215")
            recognition = db.scalar(sa.select(InvoiceRecognition).where(InvoiceRecognition.invoice_id == iid))
            assert recognition.evidence_reference == f"erp.b1b.list:{LIST_SHA}"
            assert recognition.recognized_by == str(env.party_ids[1])
            assert recognition.source_snapshot_sha256 == SNAP
            receipt = db.scalar(sa.select(AccountingReceipt).where(AccountingReceipt.document_id == iid))
            journal = db.get(JournalEntry, UUID(receipt.accounting_reference))
            assert journal.source_owner == "payables" and journal.source_document_id == str(iid)
            assert receipt.accounting_evidence_fingerprint == journal.request_fingerprint
            # The posted journal is exactly the owner's B1 consequence.
            consequence = payables.build_invoice_accounting_consequence(db, tenant_id=env.tenant_id, invoice_id=iid)
            assert receipt.consequence_fingerprint == consequence.fingerprint
            lines = {
                db.get(Account, r.account_id).code: (r.debit, r.credit)
                for r in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal.id))
            }
            assert lines["5000"] == (200, 0) and lines["2100"] == (15, 0) and lines["2000"] == (0, 215)
            assert sum(d for d, _ in lines.values()) == sum(c for _, c in lines.values()) == 215
            assert (
                db.scalar(sa.select(PaymentObligation.original_amount).where(PaymentObligation.invoice_id == iid))
                == 215
            )


def test_apply_requires_the_reviewed_plan_digest(engine, setup):
    env, legacy = setup
    ids = (imported(engine, env),)
    legacy.build(ids)
    before = counts(engine, env.tenant_id)
    with pytest.raises(cli.RecognizeRefusal, match="reviewed_plan_required"):
        legacy.run(engine, ids, apply=True)
    with pytest.raises(cli.RecognizeRefusal, match="reviewed_plan_changed"):
        legacy.run(engine, ids, apply=True, digest="0" * 64)
    assert counts(engine, env.tenant_id) == before


def test_apply_by_unknown_approver_is_refused(engine, setup):
    env, legacy = setup
    ids = (imported(engine, env),)
    legacy.build(ids)
    with pytest.raises(cli.RecognizeRefusal, match="approver_unknown"):
        legacy.run(engine, ids, apply=False, actor=UUID(int=99))


def test_rerun_is_a_no_op(engine, setup):
    env, legacy = setup
    ids = (imported(engine, env),)
    legacy.build(ids)
    plan_then_apply(engine, legacy, ids)
    after = counts(engine, env.tenant_id)
    again = legacy.run(engine, ids, apply=False)
    assert again["counts"] == dict(requested=1, eligible=0, already_done=1, refused=0)
    rerun = legacy.run(engine, ids, apply=True, digest=again["plan_digest"])
    assert rerun["applied"] == 0 and rerun["posted_total"] == "0.00"
    assert counts(engine, env.tenant_id) == after


def test_not_approved_invoice_is_refused_and_blocks_apply(engine, setup):
    env, legacy = setup
    approved, submitted = imported(engine, env, number="AP-1"), invoice(engine, env, number="AP-2")
    ids = tuple(sorted([approved, submitted], key=str))
    legacy.build(ids)
    plan = legacy.run(engine, ids, apply=False)
    assert plan["counts"]["eligible"] == 1 and plan["refusals_by_reason"] == {"not_approved": 1}
    before = counts(engine, env.tenant_id)
    with pytest.raises(cli.RecognizeRefusal, match="list_not_fully_eligible"):
        legacy.run(engine, ids, apply=True, digest=plan["plan_digest"])
    assert counts(engine, env.tenant_id) == before


def test_unknown_invoice_is_refused(engine, setup):
    env, legacy = setup
    ids = (imported(engine, env),)
    legacy.build(ids)
    plan = legacy.run(engine, (UUID(int=12345), *ids), apply=False)
    assert plan["refusals_by_reason"] == {"invoice_not_found": 1} and plan["counts"]["eligible"] == 1


def test_historical_receipt_is_refused(engine, setup):
    env, legacy = setup
    iid = invoice(engine, env)  # SUBMITTED; the owner approves it, then it is marked imported-style below
    with Session(engine) as db:
        payables.approve_invoice(
            db,
            scope=env.scope,
            invoice_id=iid,
            approval_reference="x",
            approved_by="importer",
            idempotency_key="k",
            idempotency_expires_at=None,
            approved_at=NOW,
        )
        obligation = db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == iid))
        event = db.scalar(sa.select(LiabilityEvent.id).where(LiabilityEvent.document_id == iid))
        db.add(
            HistoricalInvoiceReceipt(
                tenant_id=env.tenant_id,
                invoice_id=iid,
                obligation_id=obligation,
                event_id=event,
                source_owner="legacy-erp",
                source_invoice_id="L-1",
                source_snapshot_sha256="a" * 64,
                evidence_fingerprint="a" * 64,
                facts_fingerprint="a" * 64,
                accounting_evidence_fingerprint="a" * 64,
                source_journal_ref="j",
                destination_journal_ref="j",
                evidence={},
                original_posted_at=NOW,
                admitted_at=NOW,
            )
        )
        db.commit()
    legacy.build((iid,))
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"historical_receipt": 1}


def test_existing_owner_state_without_recognition_is_refused(engine, setup):
    env, legacy = setup
    iid = invoice(engine, env)  # SUBMITTED; the owner approves it, then it is marked imported-style below
    with Session(engine) as db:
        payables.approve_invoice(
            db,
            scope=env.scope,
            invoice_id=iid,
            approval_reference="x",
            approved_by="someone",
            idempotency_key="k",
            idempotency_expires_at=None,
            approved_at=NOW,
        )
        db.commit()
    legacy.build((iid,))
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"existing_ledger_state": 1}


def test_recognized_under_another_list_is_refused(engine, setup, monkeypatch):
    env, legacy = setup
    ids = (imported(engine, env),)
    legacy.build(ids)
    plan_then_apply(engine, legacy, ids)
    monkeypatch.setattr(cli, "evidence_reference", lambda sha: "erp.b1b.list:" + "9" * 64)
    assert legacy.run(engine, ids, apply=False)["refusals_by_reason"] == {"already_recognized": 1}


@pytest.mark.parametrize(
    "override,reason",
    [
        (dict(status="POSTED"), "source_status_not_approved"),
        (dict(posting_status="POSTED"), "source_posting_status_posted"),
        (dict(journal_entry_id="00000000-0000-0000-0000-0000000000aa"), "source_journal_present"),
        (dict(posted_at="2025-01-02T00:00:00+00:00"), "source_posted_at_present"),
        (dict(amount_paid="10"), "source_paid"),
        (dict(total_amount="999"), "source_total"),
        (dict(invoice_number="OTHER"), "header_number"),
    ],
)
def test_legacy_evidence_failures_refuse_with_named_reason(engine, setup, override, reason):
    env, legacy = setup
    iid = imported(engine, env)
    legacy.overrides[iid] = override
    legacy.build((iid,))
    plan = legacy.run(engine, (iid,), apply=False)
    assert plan["refusals_by_reason"] == {f"legacy_evidence:{reason}": 1}


def test_legacy_gl_ledger_line_or_payment_allocation_refuses(engine, setup):
    env, legacy = setup
    posted, allocated, clean = (imported(engine, env, number=n) for n in ("AP-1", "AP-2", "AP-3"))
    legacy.ledger.append(dict.fromkeys(READ_FIELDS["posted_ledger_line"]) | dict(source_document_id=str(posted)))
    legacy.allocations.append(dict(invoice_id=str(allocated)))
    ids = tuple(sorted([posted, allocated, clean], key=str))
    legacy.build(ids)
    plan = legacy.run(engine, ids, apply=False)
    assert plan["refusals_by_reason"] == {
        "legacy_evidence:source_allocations": 1,
        "legacy_evidence:source_gl_ledger_present": 1,
    }
    assert plan["counts"]["eligible"] == 1


def test_closed_period_refuses_in_plan(engine, setup):
    env, legacy = setup
    iid = imported(engine, env)
    legacy.build((iid,))
    soft_close(engine, env)
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"posting_period_closed": 1}


def test_failure_midway_rolls_back_every_invoice(engine, setup, monkeypatch):
    env, legacy = setup
    ids = tuple(sorted([imported(engine, env, number="AP-1"), imported(engine, env, number="AP-2")], key=str))
    legacy.build(ids)
    plan = legacy.run(engine, ids, apply=False)
    from erp_v2.posting import bridge
    from erp_v2.posting.rules import PostingRefused

    real, calls = bridge.post_consequence, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise PostingRefused("period_closed")
        return real(*args, **kwargs)

    monkeypatch.setattr(bridge, "post_consequence", flaky)
    with pytest.raises(cli.RecognizeRefusal, match="apply_posting_period_closed"):
        legacy.run(engine, ids, apply=True, digest=plan["plan_digest"])
    assert counts(engine, env.tenant_id) == {m.__tablename__: 0 for m in MODELS}
