"""Settled historical admission against real PostgreSQL (hosted CI only).

Real payables/accounting migrations and the real owner command ``admit_historical_settled_invoice``;
the legacy snapshot is a read-only SQLite file with the legacy column names the verifier reads. Each
scene builds an imported-style invoice (APPROVED by direct update, as the importer does), its imported
invoice journal, a legacy payment with an imported payment journal, and the matching legacy rows.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import JournalKind, JournalStatus
from dotmac_accounting.models import JournalEntry, JournalLine, PostedLedgerLine
from dotmac_kernel.audit import AuditEvent
from dotmac_payables import service as payables
from dotmac_payables.contracts import PayablesError
from dotmac_payables.models import (
    AccountingReceipt,
    HistoricalInvoiceReceipt,
    HistoricalSettlementRecord,
    InvoiceRecognition,
    LiabilityEvent,
    PaymentObligation,
    SupplierInvoice,
    SupplierInvoiceLine,
)
from sqlalchemy.orm import Session
from test_b1_supplier_invoice_post_pg import NOW, invoice, submit, world
from test_b1b_recognize_pg import grant
from test_payables_history_cli import hosted_postgres  # noqa: F401  (fixture: migrated DB, unprivileged roles)

from erp_v2.importer import payables_history_cli
from erp_v2.importer.payables_history import READ_FIELDS
from erp_v2.importer.snapshot import SnapshotManifest
from erp_v2.legacy_ap import settled_cli as cli
from erp_v2.legacy_ap.settled_verifier import ALLOCATION_FIELDS, PAYMENT_FIELDS

engine = bridge_pg.engine
LIST_SHA = "c" * 64
SNAP = "d" * 64
REVISION = "e" * 40
STAMP = "2026-01-10T00:00:00+00:00"
POSTED_AT = datetime(2026, 1, 10, tzinfo=UTC)
MODELS = (
    HistoricalInvoiceReceipt,
    HistoricalSettlementRecord,
    PaymentObligation,
    LiabilityEvent,
    InvoiceRecognition,
    AccountingReceipt,
    JournalEntry,
)
SOURCE_TABLES = {
    "organization": READ_FIELDS["organization"],
    "supplier": READ_FIELDS["supplier"],
    "supplier_invoice": READ_FIELDS["supplier_invoice"],
    "supplier_invoice_line": [*READ_FIELDS["supplier_invoice_line"], "item_id"],
    "payment_allocation": ALLOCATION_FIELDS,
    "supplier_payment": PAYMENT_FIELDS,
    "journal_entry": READ_FIELDS["journal_entry"],
    "journal_entry_line": READ_FIELDS["journal_entry_line"],
    "posted_ledger_line": READ_FIELDS["posted_ledger_line"],
    "inventory_transaction": ["source_document_type", "source_document_id", "source_document_line_id"],
}
BOOLEANS = {"is_prepayment", "is_reversal"}
INTEGERS = {"line_number"}


@pytest.fixture(scope="module", autouse=True)
def audit_registry():
    from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions

    from erp_v2.assembly import assembly
    from erp_v2.legacy_ap.audit import legacy_recognize_feature

    install_audit_actions(AuditActionRegistry.from_manifests([*assembly.modules, legacy_recognize_feature]))


def counts(engine, tenant_id) -> dict[str, int]:
    with Session(engine) as db:
        return {
            m.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == tenant_id))
            for m in MODELS
        }


def norm(value):
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def typed(column, value):
    if column.endswith("_date") and isinstance(value, str):
        return date.fromisoformat(value)
    return norm(value)


def imported_invoice(engine, env, number, approved=True):
    """An imported-style invoice: no tax, APPROVED by direct update, no event/obligation/receipt."""
    iid = invoice(engine, env, number=number, submit=False, tax="0")
    submit(engine, env, iid)
    if not approved:
        return iid
    with Session(engine) as db:
        db.execute(
            sa.update(SupplierInvoice)
            .where(SupplierInvoice.id == iid)
            .values(status="APPROVED", approved_by=str(env.party_ids[2]))
        )
        db.commit()
    return iid


def seed_journal(engine, env, *, number, document_type, document_id, lines):
    """An imported legacy journal: POSTED destination rows. Returns (journal, journal lines, ledger lines)."""
    jid = uuid4()
    total = sum(Decimal(d) for _a, d, _c in lines)
    with Session(engine) as db:
        db.add(
            JournalEntry(
                id=jid,
                tenant_id=env.tenant_id,
                fiscal_period_id=env.period_id,
                number=number,
                kind=JournalKind.STANDARD,
                status=JournalStatus.DRAFT,
                entry_date=date(2026, 1, 10),
                posting_date=date(2026, 1, 10),
                description="Legacy journal",
                currency_code="NGN",
                exchange_rate=Decimal(1),
                total_debit=total,
                total_credit=total,
                total_debit_functional=total,
                total_credit_functional=total,
                source_owner="legacy-erp",
                source_document_kind=document_type,
                source_document_id=f"legacy:gl.journal_entry:{jid}",
                source_version=REVISION,
                source_fingerprint="a" * 64,
                request_fingerprint="b" * 64,
                approval_reference=f"legacy:gl.journal_entry:{jid}@2026-01-10",
                posted_by="legacy-poster",
                posted_at=POSTED_AT,
            )
        )
        db.flush()
        for n, (account, debit, credit) in enumerate(lines, start=1):
            line = JournalLine(
                tenant_id=env.tenant_id,
                journal_id=jid,
                line_number=n,
                account_id=env.accounts[account],
                description=None,
                debit=Decimal(debit),
                credit=Decimal(credit),
                debit_functional=Decimal(debit),
                credit_functional=Decimal(credit),
            )
            db.add(line)
        db.flush()
        for line in db.scalars(sa.select(JournalLine).where(JournalLine.journal_id == jid)):
            account = next(code for code, ident in env.accounts.items() if ident == line.account_id)
            db.add(
                PostedLedgerLine(
                    tenant_id=env.tenant_id,
                    journal_id=jid,
                    journal_line_id=line.id,
                    fiscal_period_id=env.period_id,
                    account_id=line.account_id,
                    account_code=account,
                    journal_number=number,
                    entry_date=date(2026, 1, 10),
                    posting_date=date(2026, 1, 10),
                    description=None,
                    currency_code="NGN",
                    debit=line.debit,
                    credit=line.credit,
                    original_debit=line.debit,
                    original_credit=line.credit,
                    exchange_rate=Decimal(1),
                    source_owner="legacy-erp",
                    source_document_kind=document_type,
                    source_document_id=document_id,
                    source_version=REVISION,
                    source_fingerprint="c" * 64,
                    posted_by="legacy-poster",
                    posted_at=POSTED_AT,
                )
            )
        db.flush()
        db.execute(sa.update(JournalEntry).where(JournalEntry.id == jid).values(status=JournalStatus.POSTED))
        db.commit()
        journal = db.get(JournalEntry, jid)
        jlines = list(db.scalars(sa.select(JournalLine).where(JournalLine.journal_id == jid)))
        ledger = list(db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == jid)))
        db.expunge_all()
    return journal, jlines, ledger


def legacy_journal(env, journal, document_type, document_id, **override):
    row = dict.fromkeys(READ_FIELDS["journal_entry"])
    row.update(
        journal_entry_id=str(journal.id),
        organization_id=str(env.tenant_id),
        journal_number=journal.number,
        journal_type="STANDARD",
        status="POSTED",
        source_module="AP",
        source_document_type=document_type,
        source_document_id=str(document_id),
        is_reversal=False,
        currency_code="NGN",
        exchange_rate="1",
        entry_date="2026-01-10",
        posting_date="2026-01-10",
        fiscal_period_id=str(journal.fiscal_period_id),
        description=journal.description,
        reference=None,
        total_debit=str(journal.total_debit),
        total_credit=str(journal.total_credit),
        total_debit_functional=str(journal.total_debit_functional),
        total_credit_functional=str(journal.total_credit_functional),
        posted_at=journal.posted_at.isoformat(),
        posted_by_user_id=journal.posted_by,
        created_at=journal.created_at.isoformat(),
    )
    row.update(override)
    return row


def legacy_ledger(env, ledger, document_type, document_id):
    rows = []
    for line in ledger:
        row = dict.fromkeys(READ_FIELDS["posted_ledger_line"])
        row.update(
            ledger_line_id=str(line.id),
            organization_id=str(env.tenant_id),
            journal_entry_id=str(line.journal_id),
            journal_line_id=str(line.journal_line_id),
            account_id=str(line.account_id),
            account_code=line.account_code,
            fiscal_period_id=str(line.fiscal_period_id),
            entry_date="2026-01-10",
            posting_date="2026-01-10",
            debit_amount=str(line.debit),
            credit_amount=str(line.credit),
            original_currency_code="NGN",
            original_debit_amount=str(line.debit),
            original_credit_amount=str(line.credit),
            exchange_rate="1",
            source_module="AP",
            source_document_type=document_type,
            source_document_id=str(document_id),
            posted_by_user_id=line.posted_by,
            posted_at=line.posted_at.isoformat(),
        )
        rows.append(row)
    return rows


def legacy_journal_lines(jlines):
    rows = []
    for line in jlines:
        row = dict.fromkeys(READ_FIELDS["journal_entry_line"])
        row.update(
            line_id=str(line.id),
            journal_entry_id=str(line.journal_id),
            line_number=line.line_number,
            account_id=str(line.account_id),
            debit_amount=str(line.debit),
            credit_amount=str(line.credit),
            debit_amount_functional=str(line.debit_functional),
            credit_amount_functional=str(line.credit_functional),
            currency_code="NGN",
            exchange_rate="1",
            created_at=line.created_at.isoformat(),
        )
        rows.append(row)
    return rows


class Scene:
    """Legacy snapshot plus imported destination rows for a set of partially paid invoices."""

    def __init__(self, engine, env, tmp_path):
        self.engine, self.env, self.path = engine, env, tmp_path / "source.db"
        self.rows = {name: [] for name in SOURCE_TABLES}
        self.invoices: list[UUID] = []
        self.payments: dict[UUID, UUID] = {}
        self.allocations: dict[UUID, UUID] = {}
        self.journals: dict[UUID, UUID] = {}
        with engine.connect() as dest:
            self.destination_name = dest.scalar(sa.text("SELECT current_database()"))
        self.rows["organization"].append(dict(organization_id=str(env.tenant_id), functional_currency_code="NGN"))

    def add(self, number, *, paid="80", payment_amount=None, withholding="0", dest_payment_journal=True, approved=True):
        """One invoice of total 200 partially paid by one cleared payment (net + withholding = paid)."""
        env, engine = self.env, self.engine
        iid = imported_invoice(engine, env, number, approved)
        jinv, jinv_lines, jinv_ledger = seed_journal(
            engine,
            env,
            number=f"JE-{number}",
            document_type="SUPPLIER_INVOICE",
            document_id=iid,
            lines=[("5000", "200", "0"), ("2000", "0", "200")],
        )
        pid, aid = uuid4(), uuid4()
        net = payment_amount if payment_amount is not None else str(Decimal(paid) - Decimal(withholding))
        if dest_payment_journal:
            jpay, jpay_lines, jpay_ledger = seed_journal(
                engine,
                env,
                number=f"PJ-{number}",
                document_type="SUPPLIER_PAYMENT",
                document_id=pid,
                lines=[("2000", paid, "0"), ("2040", "0", paid)],
            )
        else:
            jpay = SupplierInvoice  # placeholder never read
        with Session(engine) as db:
            inv = db.get(SupplierInvoice, iid)
            lines = list(db.scalars(sa.select(SupplierInvoiceLine).where(SupplierInvoiceLine.invoice_id == iid)))
            db.expunge_all()
        header = dict.fromkeys(READ_FIELDS["supplier_invoice"])
        header.update(
            organization_id=str(env.tenant_id),
            invoice_id=str(iid),
            supplier_id=inv.supplier_ref,
            invoice_number=inv.number,
            supplier_invoice_number=inv.supplier_document_number,
            invoice_type="STANDARD",
            status="PARTIALLY_PAID",
            posting_status="POSTED",
            invoice_date=inv.invoice_date.isoformat(),
            received_date=inv.received_date.isoformat(),
            due_date=inv.payment_schedule[0]["due_date"],
            currency_code="NGN",
            exchange_rate="1",
            subtotal=str(inv.subtotal),
            tax_amount=str(inv.tax_amount),
            total_amount=str(inv.total_amount),
            amount_paid=paid,
            functional_currency_amount=str(inv.total_amount),
            ap_control_account_id=inv.liability_account_ref,
            journal_entry_id=str(jinv.id),
            posted_at=STAMP,
            withholding_tax_amount="0",
            prepayment_applied="0",
            is_prepayment=False,
            stamp_duty_amount="0",
            submitted_by_user_id=inv.submitted_by,
            submitted_at=norm(inv.submitted_at),
            approved_by_user_id=inv.approved_by,
            approved_at=norm(inv.approved_at),
            approval_request_id=inv.approval_reference,
            created_at=inv.created_at.isoformat(),
        )
        self.rows["supplier_invoice"].append(header)
        self.rows["supplier"].append(
            dict(
                supplier_id=inv.supplier_ref,
                organization_id=str(env.tenant_id),
                legal_name=inv.supplier_name_snapshot,
                trading_name=None,
            )
        ) if not any(r["supplier_id"] == inv.supplier_ref for r in self.rows["supplier"]) else None
        for line in lines:
            row = dict.fromkeys(READ_FIELDS["supplier_invoice_line"]) | {"item_id": None}
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
                created_at=line.created_at.isoformat(),
            )
            self.rows["supplier_invoice_line"].append(row)
        self.rows["journal_entry"].append(legacy_journal(env, jinv, "SUPPLIER_INVOICE", iid))
        self.rows["journal_entry_line"] += legacy_journal_lines(jinv_lines)
        self.rows["posted_ledger_line"] += legacy_ledger(env, jinv_ledger, "SUPPLIER_INVOICE", iid)
        payment = dict.fromkeys(PAYMENT_FIELDS)
        payment.update(
            payment_id=str(pid),
            organization_id=str(env.tenant_id),
            supplier_id=inv.supplier_ref,
            status="CLEARED",
            payment_date="2026-01-12",
            currency_code="NGN",
            amount=net,
            exchange_rate="1",
            functional_currency_amount=net,
            gross_amount=None,
            withholding_tax_amount=withholding,
            journal_entry_id=str(jpay.id) if dest_payment_journal else str(uuid4()),
        )
        self.rows["supplier_payment"].append(payment)
        self.rows["payment_allocation"].append(
            dict(
                allocation_id=str(aid),
                payment_id=str(pid),
                invoice_id=str(iid),
                allocated_amount=paid,
                discount_taken="0",
                exchange_difference="0",
                allocation_date="2026-01-12",
            )
        )
        if dest_payment_journal:
            self.rows["journal_entry"].append(legacy_journal(env, jpay, "SUPPLIER_PAYMENT", pid))
            self.rows["journal_entry_line"] += legacy_journal_lines(jpay_lines)
            self.rows["posted_ledger_line"] += legacy_ledger(env, jpay_ledger, "SUPPLIER_PAYMENT", pid)
        else:
            ghost = uuid4()
            payment["journal_entry_id"] = str(ghost)
            fake = type("J", (), dict(id=ghost, number="PJ-ghost", fiscal_period_id=env.period_id, description="x"))
            fake.total_debit = fake.total_credit = fake.total_debit_functional = fake.total_credit_functional = paid
            fake.posted_at, fake.posted_by, fake.created_at = POSTED_AT, "legacy-poster", POSTED_AT
            self.rows["journal_entry"].append(legacy_journal(env, fake, "SUPPLIER_PAYMENT", pid))
            ghost_ledger = [
                type(
                    "L",
                    (),
                    dict(
                        id=uuid4(),
                        journal_id=ghost,
                        journal_line_id=uuid4(),
                        account_id=env.accounts[code],
                        account_code=code,
                        fiscal_period_id=env.period_id,
                        debit=Decimal(d),
                        credit=Decimal(c),
                        posted_by="legacy-poster",
                        posted_at=POSTED_AT,
                    ),
                )
                for code, d, c in (("2000", paid, "0"), ("2040", "0", paid))
            ]
            self.rows["posted_ledger_line"] += legacy_ledger(env, ghost_ledger, "SUPPLIER_PAYMENT", pid)
        self.invoices.append(iid)
        self.payments[iid], self.allocations[iid], self.journals[iid] = (
            pid,
            aid,
            jpay.id if dest_payment_journal else ghost,
        )
        return iid

    def edit(self, table, key, value, **changes):
        row = next(r for r in self.rows[table] if r[key] == str(value))
        row.update(changes)

    def build(self):
        self.path.unlink(missing_ok=True)
        source = sa.create_engine(f"sqlite:///{self.path}")
        with source.begin() as conn:
            for name, fields in SOURCE_TABLES.items():
                table = sa.Table(
                    name,
                    sa.MetaData(),
                    *(
                        sa.Column(
                            f,
                            sa.Boolean
                            if f in BOOLEANS
                            else sa.Integer
                            if f in INTEGERS
                            else sa.Date
                            if f.endswith("_date")
                            else sa.Text,
                        )
                        for f in fields
                    ),
                )
                table.create(conn)
                if self.rows[name]:
                    conn.execute(table.insert(), [{k: typed(k, v) for k, v in row.items()} for row in self.rows[name]])
        source.dispose()

    def manifest(self):
        return SnapshotManifest(SNAP, "source.db", self.destination_name, REVISION, (str(self.env.tenant_id),))

    def run(self, ids, *, apply, digest=None, actor=None, now=NOW, plan_sink=None, referenced=frozenset()):
        source = sa.create_engine(f"sqlite:///{self.path}")
        try:
            with source.connect() as src:
                src.exec_driver_sql("PRAGMA query_only=1")
                with Session(self.engine) as db, db.begin():
                    if not apply:
                        db.execute(sa.text("SET TRANSACTION READ ONLY"))
                    return cli.run_cohort(
                        db,
                        src,
                        self.manifest(),
                        tenant=self.env.tenant_id,
                        ids=tuple(sorted(ids, key=str)),
                        list_sha256=LIST_SHA,
                        actor_party_id=actor or self.env.party_ids[1],
                        apply=apply,
                        reviewed_digest=digest,
                        bindings=dict(list_sha256=LIST_SHA),
                        now=now,
                        plan_sink=plan_sink,
                        referenced=referenced,
                    )
        finally:
            source.dispose()

    def plan_then_apply(self, ids):
        plan = self.run(ids, apply=False)
        return plan, self.run(ids, apply=True, digest=plan["plan_digest"])


@pytest.fixture
def setup(engine, tmp_path):
    env = world(engine)
    grant(engine, env, env.party_ids[1])  # approver; submitter is party 0, legacy approver party 2
    return env, Scene(engine, env, tmp_path)


def one(engine, setup_, **kw):
    env, scene = setup_
    iid = scene.add("AP-1", **kw)
    scene.build()
    return env, scene, iid


def test_plan_is_read_only_and_reports_counts_and_money_totals_without_ids(engine, setup):
    env, scene = setup
    ids = [scene.add("AP-1"), scene.add("AP-2", paid="50")]
    scene.build()
    before = counts(engine, env.tenant_id)
    plan = scene.run(ids, apply=False)  # inside SET TRANSACTION READ ONLY
    assert plan["mode"] == "plan" and plan["counts"] == dict(requested=2, eligible=2, already_done=0, refused=0)
    assert plan["totals"]["eligible"] == dict(
        total="400.00", settled="130.00", withholding="0.00", residual="270.00", settlements=2
    )
    assert before == counts(engine, env.tenant_id)
    assert before["historical_settlements"] == before["payment_obligations"] == before["liability_events"] == 0
    text = json.dumps(plan)
    identifiers = [str(i) for i in ids]
    identifiers += [row["allocation_id"] for row in scene.rows["payment_allocation"]]
    identifiers += [row["payment_id"] for row in scene.rows["supplier_payment"]]
    identifiers += [row["journal_entry_id"] for row in scene.rows["supplier_payment"]]
    assert not any(i in text for i in identifiers)


def test_apply_admits_the_settled_ledger_with_exact_amounts_and_journal_refs(engine, setup):
    env, scene, iid = one(engine, setup, paid="80")
    plan, applied = scene.plan_then_apply([iid])
    assert applied["applied"] == 1 and applied["plan_digest"] == plan["plan_digest"]
    assert applied["totals"]["eligible"]["residual"] == "120.00"
    with Session(engine) as db:
        obligation = db.scalar(sa.select(PaymentObligation).where(PaymentObligation.invoice_id == iid))
        assert obligation.original_amount == Decimal("200") and obligation.outstanding_amount == Decimal("120")
        assert obligation.status.value == "partially_settled" or str(obligation.status).endswith("PARTIALLY_SETTLED")
        (row,) = db.scalars(
            sa.select(HistoricalSettlementRecord).where(HistoricalSettlementRecord.obligation_id == obligation.id)
        )
        assert row.applied_amount == Decimal("80") and row.withholding_amount == 0
        assert row.source_settlement_id == str(scene.allocations[iid])
        assert row.payment_ref == str(scene.payments[iid])
        assert row.source_journal_ref == row.destination_journal_ref == str(scene.journals[iid])
        kinds = sorted(
            e.event_kind
            for e in db.scalars(sa.select(LiabilityEvent).where(LiabilityEvent.document_id.in_([iid, row.id])))
        )
        assert kinds == ["historical_invoice_recognized", "historical_settlement_recorded"]
        events = list(db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == env.tenant_id)))
        assert [e.action for e in events if "settled_admitted" in e.action] == [
            "erp.finance.supplier_invoice.legacy_settled_admitted"
        ]
    now = counts(engine, env.tenant_id)
    # No accounting consequence: only the two seeded journals (invoice, payment) exist; no receipt.
    assert now["accounting_receipts"] == 0 and now["journal_entries"] == 2 and now["invoice_recognitions"] == 0


def test_withholding_is_attributed_to_the_single_allocation_and_counts_as_settled(engine, setup):
    env, scene, iid = one(engine, setup, paid="80", withholding="8")
    _plan, applied = scene.plan_then_apply([iid])
    assert applied["totals"]["eligible"]["withholding"] == "8.00"
    with Session(engine) as db:
        row = db.scalar(
            sa.select(HistoricalSettlementRecord).where(HistoricalSettlementRecord.tenant_id == env.tenant_id)
        )
        assert row.applied_amount == Decimal("80") and row.withholding_amount == Decimal("8")


def test_apply_requires_the_reviewed_plan_digest(engine, setup):
    env, scene, iid = one(engine, setup)
    for digest in (None, "0" * 64):
        with pytest.raises(cli.SettledRefusal) as exc:
            scene.run([iid], apply=True, digest=digest)
        assert exc.value.reason in {"reviewed_plan_required", "reviewed_plan_changed"}
    assert counts(engine, env.tenant_id)["historical_settlements"] == 0


def test_unknown_or_unprivileged_approver_is_refused(engine, setup):
    env, scene, iid = one(engine, setup)
    with pytest.raises(cli.SettledRefusal, match="approver_unknown"):
        scene.run([iid], apply=False, actor=uuid4())
    with pytest.raises(cli.SettledRefusal, match="approver_lacks_finance_approver"):
        scene.run([iid], apply=False, actor=env.party_ids[0])


def test_rerun_is_a_no_op_even_though_historical_settlements_is_no_longer_empty(engine, setup):
    env, scene, iid = one(engine, setup)
    _plan, _ = scene.plan_then_apply([iid])
    after_first = counts(engine, env.tenant_id)
    assert after_first["historical_settlements"] == 1
    again = scene.run([iid], apply=False)
    assert again["counts"] == dict(requested=1, eligible=0, already_done=1, refused=0)
    applied = scene.run([iid], apply=True, digest=again["plan_digest"])
    assert applied["applied"] == 0 and counts(engine, env.tenant_id) == after_first


def test_settlements_of_any_other_invoice_refuse_the_run(engine, setup):
    _env, scene = setup
    first, second = scene.add("AP-1"), scene.add("AP-2")
    scene.build()
    scene.plan_then_apply([first])
    with pytest.raises(cli.SettledRefusal, match="destination_settlements_outside_list"):
        scene.run([second], apply=False)


def refused(scene, ids, reason, **kw):
    plan = scene.run(ids, apply=False, **kw)
    assert plan["refusals_by_reason"] == {reason: len(ids)}, plan
    with pytest.raises(cli.SettledRefusal, match="list_not_fully_eligible"):
        scene.run(ids, apply=True, digest=plan["plan_digest"], **kw)
    return plan


def test_unposted_legacy_payment_journal_is_refused(engine, setup):
    env, scene, iid = one(engine, setup)
    scene.edit("journal_entry", "journal_entry_id", scene.journals[iid], status="DRAFT")
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_journal_not_posted")
    assert counts(engine, env.tenant_id)["historical_settlements"] == 0


def test_payment_that_is_not_cleared_or_has_no_journal_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    scene.edit("supplier_payment", "payment_id", scene.payments[iid], status="SENT")
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_not_posted")


def test_payment_without_a_journal_id_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    scene.edit("supplier_payment", "payment_id", scene.payments[iid], journal_entry_id=None)
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_journal_missing")


def test_payment_journal_not_imported_to_the_destination_is_refused(engine, setup):
    _env, scene = setup
    iid = scene.add("AP-1", dest_payment_journal=False)
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_destination_journal_missing")


def test_reversed_legacy_payment_journal_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    scene.edit("journal_entry", "journal_entry_id", scene.journals[iid], reversal_journal_id=str(uuid4()))
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_journal_reversed")


def test_allocation_sum_not_equal_to_paid_amount_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    scene.edit("supplier_invoice", "invoice_id", iid, amount_paid="70")
    scene.build()
    refused(scene, [iid], "legacy_evidence:allocation_sum_mismatch")


def test_payment_allocations_not_equal_to_payment_gross_are_refused(engine, setup):
    _env, scene = setup
    iid = scene.add("AP-1", payment_amount="75")  # net 75 + withholding 0 != allocated 80
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_allocation_sum_mismatch")


def test_payment_gl_debit_not_matching_the_allocations_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    for row in scene.rows["posted_ledger_line"]:
        if row["source_document_type"] == "SUPPLIER_PAYMENT" and row["account_code"] == "2000":
            row["debit_amount"] = "79"
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_ledger_unbalanced")


def test_fully_paid_invoice_has_no_open_residual_and_is_refused(engine, setup):
    _env, scene = setup
    iid = scene.add("AP-1", paid="200")
    scene.build()
    refused(scene, [iid], "legacy_evidence:invoice_not_partially_paid")


def test_amount_with_more_than_two_decimals_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    scene.edit("payment_allocation", "invoice_id", iid, allocated_amount="80.005")
    scene.edit("supplier_invoice", "invoice_id", iid, amount_paid="80.005")
    scene.build()
    plan = scene.run([iid], apply=False)
    assert plan["refusals_by_reason"] == {"legacy_evidence:allocation_amount_scale": 1}


@pytest.mark.parametrize(
    "table,key,changes,reason",
    [
        ("supplier_invoice", "invoice_id", dict(currency_code="USD"), "unsupported_currency"),
        ("supplier_invoice", "invoice_id", dict(exchange_rate="2"), "unsupported_currency"),
        ("supplier_invoice", "invoice_id", dict(status="DRAFT"), "source_status"),
        ("supplier_invoice", "invoice_id", dict(withholding_tax_amount="1"), "source_withholding"),
        ("payment_allocation", "invoice_id", dict(discount_taken="1"), "allocation_discount_or_fx"),
        ("payment_allocation", "invoice_id", dict(exchange_difference="1"), "allocation_discount_or_fx"),
        ("supplier_payment", "payment_id", dict(currency_code="USD"), "unsupported_currency"),
        ("supplier_payment", "payment_id", dict(supplier_id="someone-else"), "payment_supplier_mismatch"),
        ("supplier_payment", "payment_id", dict(exchange_rate="2"), "unsupported_currency"),
        ("supplier_invoice_line", "invoice_id", dict(po_line_id=str(uuid4())), "unsupported_procurement_link"),
        ("supplier_invoice_line", "invoice_id", dict(item_id=str(uuid4())), "source_item_link"),
    ],
)
def test_legacy_evidence_failures_refuse_with_named_reasons(engine, setup, table, key, changes, reason):
    _env, scene, iid = one(engine, setup)
    ident = {"payment_id": scene.payments[iid]}.get(key, iid)
    scene.edit(table, key, ident, **changes)
    scene.build()
    plan = scene.run([iid], apply=False)
    assert plan["refusals_by_reason"] == {"legacy_evidence:" + reason: 1}, plan


def test_debit_note_or_adjustment_on_control_citing_the_invoice_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    inv_lines = [
        r
        for r in scene.rows["posted_ledger_line"]
        if r["source_document_type"] == "SUPPLIER_INVOICE" and r["source_document_id"] == str(iid)
    ]
    control = next(r for r in inv_lines if r["credit_amount"] != "0.000000" and r["account_code"] == "2000")
    scene.rows["posted_ledger_line"].append(
        control | dict(ledger_line_id=str(uuid4()), journal_entry_id=str(uuid4()), journal_line_id=str(uuid4()))
    )
    scene.build()
    refused(scene, [iid], "legacy_evidence:invoice_control_other_postings")


def _payment_line(scene, iid):
    return next(r for r in scene.rows["journal_entry_line"] if r["journal_entry_id"] == str(scene.journals[iid]))


def test_payment_journal_lines_must_pair_and_be_one_sided_and_nonnegative(engine, setup):
    _env, scene, iid = one(engine, setup)
    _payment_line(scene, iid).update(debit_amount="-80", credit_amount="0")
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_journal_line_amounts")


def test_payment_journal_line_missing_from_the_legacy_side_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    scene.rows["journal_entry_line"].remove(_payment_line(scene, iid))
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_journal_line_count")


def test_payment_journal_with_a_wrong_posting_stamp_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    scene.edit("journal_entry", "journal_entry_id", scene.journals[iid], posting_date="2026-01-11")
    scene.build()
    refused(scene, [iid], "legacy_evidence:payment_journal_posting_date")


def test_inventory_transaction_citing_the_invoice_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    scene.rows["inventory_transaction"].append(
        dict(source_document_type="SUPPLIER_INVOICE", source_document_id=str(iid), source_document_line_id=None)
    )
    scene.build()
    refused(scene, [iid], "legacy_evidence:source_inventory_transaction")


def test_withholding_payment_spanning_two_allocations_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup, paid="80", withholding="8")
    scene.rows["payment_allocation"].append(
        dict(
            allocation_id=str(uuid4()),
            payment_id=str(scene.payments[iid]),
            invoice_id=str(uuid4()),
            allocated_amount="0",
            discount_taken="0",
            exchange_difference="0",
            allocation_date="2026-01-12",
        )
    )
    scene.build()
    plan = scene.run([iid], apply=False)
    assert plan["refusals_by_reason"] == {"legacy_evidence:payment_withholding_split_unsupported": 1}


def test_destination_invoice_must_be_approved_and_free_of_owner_state(engine, setup):
    env, scene = setup
    submitted, held, good = scene.add("AP-1", approved=False), scene.add("AP-2"), scene.add("AP-3")
    scene.build()
    with Session(engine) as db:
        db.add(
            PaymentObligation(
                tenant_id=env.tenant_id,
                invoice_id=held,
                sequence=1,
                due_date=date(2026, 2, 28),
                currency_code="NGN",
                original_amount=Decimal("200"),
                outstanding_amount=Decimal("200"),
            )
        )
        db.commit()
    plan = scene.run([submitted, held, good], apply=False)
    assert plan["refusals_by_reason"] == {"not_approved": 1, "existing_ledger_state": 1}
    assert plan["counts"]["eligible"] == 1


def test_unknown_invoice_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    plan = scene.run([iid, uuid4()], apply=False)
    assert plan["refusals_by_reason"] == {"invoice_not_found": 1}


def test_invoice_reentered_under_a_legacy_reference_is_refused(engine, setup):
    _env, scene, iid = one(engine, setup)
    plan = scene.run([iid], apply=False, referenced=frozenset({str(iid)}))
    assert plan["refusals_by_reason"] == {"legacy_reference_entered": 1}


def test_one_legacy_allocation_cannot_settle_two_invoices_in_one_list(engine, setup):
    _env, scene = setup
    first, second = scene.add("AP-1"), scene.add("AP-2")
    # Both settlements cite the same legacy allocation id (A5-2).
    scene.edit("payment_allocation", "invoice_id", second, allocation_id=str(scene.allocations[first]))
    scene.build()
    plan = scene.run([first, second], apply=False)
    assert plan["refusals_by_reason"] == {"settlement_id_duplicate": 2} and plan["counts"]["eligible"] == 0


def test_a_settlement_already_admitted_against_another_invoice_is_refused(engine, setup):
    _env, scene = setup
    first, second = scene.add("AP-1"), scene.add("AP-2")
    scene.edit("payment_allocation", "invoice_id", second, allocation_id=str(scene.allocations[first]))
    scene.build()
    scene.plan_then_apply([first])
    # A list holding both invoices keeps the admitted one in-list, so the second reaches the owner check.
    plan = scene.run([first, second], apply=False)
    assert plan["counts"]["already_done"] == 1
    assert plan["refusals_by_reason"] == {"settlement_already_admitted": 1}


def test_apply_changing_an_amount_is_refused_and_rolls_everything_back(engine, setup, monkeypatch):
    env, scene = setup
    ids = [scene.add("AP-1"), scene.add("AP-2")]
    scene.build()
    plan = scene.run(ids, apply=False)
    real = payables.admit_historical_settled_invoice
    calls = []

    def tampering(db, **kw):
        receipt = real(db, **kw)
        calls.append(1)
        if len(calls) == 2:
            db.execute(
                sa.update(PaymentObligation)
                .where(PaymentObligation.invoice_id == kw["command"].invoice_id)
                .values(outstanding_amount=Decimal("1"))
            )
        return receipt

    import dotmac_payables

    monkeypatch.setattr(dotmac_payables, "admit_historical_settled_invoice", tampering)
    with pytest.raises(cli.SettledRefusal) as exc:
        scene.run(ids, apply=True, digest=plan["plan_digest"])
    assert exc.value.reason == "apply_amounts_changed"
    assert counts(engine, env.tenant_id)["historical_settlements"] == 0
    assert counts(engine, env.tenant_id)["payment_obligations"] == 0


def test_failure_midway_rolls_back_every_invoice(engine, setup, monkeypatch):
    env, scene = setup
    ids = [scene.add("AP-1"), scene.add("AP-2")]
    scene.build()
    plan = scene.run(ids, apply=False)
    real = payables.admit_historical_settled_invoice
    calls = []

    def failing(db, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise PayablesError("boom")
        return real(db, **kw)

    import dotmac_payables

    monkeypatch.setattr(dotmac_payables, "admit_historical_settled_invoice", failing)
    with pytest.raises(cli.SettledRefusal, match="apply_payables_refused"):
        scene.run(ids, apply=True, digest=plan["plan_digest"])
    assert counts(engine, env.tenant_id)["historical_settlements"] == 0


def test_advisory_lock_is_taken_before_any_invoice_is_read_and_only_for_apply(engine, setup):
    _env, scene, iid = one(engine, setup)
    plan = scene.run([iid], apply=False)
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        scene.run([iid], apply=True, digest=plan["plan_digest"])
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    lock = next(i for i, text in enumerate(statements) if "pg_advisory_xact_lock" in text)
    first_read = next(i for i, text in enumerate(statements) if "supplier_invoices" in text)
    assert lock < first_read
    statements.clear()
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        scene.run([iid], apply=False)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert not any("pg_advisory_xact_lock" in text for text in statements)


def test_advisory_lock_key_is_the_tenant_not_the_list(engine, setup):
    env, scene, iid = one(engine, setup)
    plan = scene.run([iid], apply=False)
    keys: list = []

    def record(conn, cursor, statement, parameters, *args):
        if "pg_advisory_xact_lock" in statement:
            keys.append(parameters)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        scene.run([iid], apply=True, digest=plan["plan_digest"])
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert keys and "erp.settled:" + str(env.tenant_id) in str(keys) and LIST_SHA not in str(keys)


def test_receipt_has_no_ids_and_the_private_plan_has_the_detail(engine, setup):
    _env, scene = setup
    good, bad = scene.add("AP-1"), scene.add("AP-2")
    scene.edit("supplier_payment", "payment_id", scene.payments[bad], status="SENT")
    scene.build()
    captured = []
    receipt = scene.run([good, bad], apply=False, plan_sink=captured.append)
    (private,) = captured
    assert private["plan_digest"] == receipt["plan_digest"]
    rows = {row["invoice_id"]: row for row in private["invoices"]}
    assert rows[str(good)] == dict(
        invoice_id=str(good),
        outcome="eligible",
        reason=None,
        evidence_fingerprint=rows[str(good)]["evidence_fingerprint"],
        total="200.00",
        settled="80.00",
        withholding="0.00",
        residual="120.00",
        settlements=1,
    )
    assert rows[str(bad)]["outcome"] == "refused" and rows[str(bad)]["reason"] == "legacy_evidence:payment_not_posted"
    text = json.dumps(receipt)
    for row in scene.rows["payment_allocation"] + scene.rows["supplier_payment"]:
        assert not any(str(v) in text for v in row.values() if v and len(str(v)) >= 32)
    assert str(good) not in text and str(bad) not in text
    assert "settlement_id" not in text and "payment_ref" not in text


# --- the inherited preflight's empty-table rule, on the migrated, unprivileged hosted database -------------


def test_settled_rule_exempts_only_historical_settlements():
    assert set(payables_history_cli.PREIMPORT_EMPTY_TABLES) - set(
        payables_history_cli.SETTLED_PREIMPORT_EMPTY_TABLES
    ) == {"historical_settlements"}


@pytest.mark.parametrize("table", payables_history_cli.PREIMPORT_EMPTY_TABLES)
def test_preflight_empty_rule_default_and_settled(hosted_postgres, monkeypatch, table):  # noqa: F811
    from uuid import UUID as U

    engines, _names = hosted_postgres
    tenant = U("00000000-0000-0000-0000-000000000001")
    monkeypatch.setitem(
        payables_history_cli._EMPTY_TABLE_COUNT_SQL, table, "SELECT 1 WHERE CAST(:tenant AS text) IS NOT NULL"
    )
    with Session(engines["operator"]) as session, session.begin():
        with pytest.raises(payables_history_cli.OperatorRefusal, match="destination_payables_history_not_empty"):
            payables_history_cli._destination_preflight(session, tenant)
    with Session(engines["operator"]) as session, session.begin():
        try:
            payables_history_cli._destination_preflight(
                session, tenant, payables_history_cli.SETTLED_PREIMPORT_EMPTY_TABLES
            )
            allowed = True
        except payables_history_cli.OperatorRefusal as exc:
            assert exc.reason == "destination_payables_history_not_empty"
            allowed = False
    assert allowed == (table == "historical_settlements")


def test_preflight_rejects_a_table_outside_the_known_set(hosted_postgres):  # noqa: F811
    from uuid import UUID as U

    engines, _names = hosted_postgres
    with Session(engines["operator"]) as session, session.begin():
        with pytest.raises(payables_history_cli.OperatorRefusal, match="preflight_unknown_table"):
            payables_history_cli._destination_preflight(session, U(int=1), ("supplier_invoices",))
