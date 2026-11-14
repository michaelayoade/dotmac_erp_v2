"""Read-only evidence near misses, using historical column names and real SQL."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer.payables_history import HistoricalEvidenceError, HistoricalPayablesVerifier
from erp_v2.importer.snapshot import SnapshotManifest

T, INVOICE, S, J, AP, EXP, PERIOD = (str(UUID(int=n)) for n in range(1, 8))
STAMP = "2025-01-02T10:00:00+00:00"
DAY = "2025-01-02"


def facts():
    source = {
        "organization": [dict(organization_id=T, functional_currency_code="NGN")],
        "supplier": [dict(supplier_id=S, organization_id=T, legal_name="Actual supplier", trading_name=None)],
        "supplier_invoice": [
            dict(
                organization_id=T,
                invoice_id=INVOICE,
                supplier_id=S,
                invoice_number="AP1",
                supplier_invoice_number=None,
                invoice_type="STANDARD",
                status="POSTED",
                posting_status="POSTED",
                invoice_date=DAY,
                received_date=DAY,
                due_date=DAY,
                currency_code="NGN",
                exchange_rate="1",
                subtotal="100.000000",
                tax_amount="0.000000",
                total_amount="100.000000",
                amount_paid="0",
                functional_currency_amount="100",
                ap_control_account_id=AP,
                journal_entry_id=J,
                posted_at=STAMP,
                withholding_tax_amount="0",
                withholding_tax_code_id=None,
                prepayment_applied="0",
                is_prepayment=False,
                stamp_duty_amount="0",
                submitted_by_user_id=None,
                submitted_at=None,
                approved_by_user_id=None,
                approved_at=None,
                approval_request_id=None,
            )
        ],
        "supplier_invoice_line": [
            dict(
                line_id=str(UUID(int=11)),
                invoice_id=INVOICE,
                line_number=1,
                description="Inclusive line",
                quantity="3",
                unit_price="35",
                line_amount="100",
                tax_amount="0",
                expense_account_id=EXP,
                cost_center_id=None,
                project_id=None,
                segment_id=None,
                po_line_id=None,
                goods_receipt_line_id=None,
            )
        ],
        "payment_allocation": [],
        "journal_entry": [
            dict(
                journal_entry_id=J,
                organization_id=T,
                journal_number="JE1",
                journal_type="STANDARD",
                status="POSTED",
                source_module="AP",
                source_document_type="SUPPLIER_INVOICE",
                source_document_id=INVOICE,
                is_reversal=False,
                reversed_journal_id=None,
                reversal_journal_id=None,
                currency_code="NGN",
                exchange_rate="1",
                entry_date=DAY,
                posting_date=DAY,
                fiscal_period_id=PERIOD,
                description="Original invoice",
                reference=None,
                total_debit="100",
                total_credit="100",
                total_debit_functional="100",
                total_credit_functional="100",
                posted_at=STAMP,
                posted_by_user_id=S,
            )
        ],
        "journal_entry_line": [],
        "posted_ledger_line": [],
    }
    target = {
        "supplier_invoices": [
            dict(
                tenant_id=T,
                id=INVOICE,
                number="AP1",
                supplier_ref=S,
                supplier_name_snapshot="Actual supplier",
                supplier_document_number=None,
                invoice_date=DAY,
                received_date=DAY,
                currency_code="NGN",
                exchange_rate="1",
                subtotal="100",
                tax_amount="0",
                total_amount="100",
                liability_account_ref=AP,
                payment_schedule=[dict(due_date=DAY, amount="100.000000")],
                procurement_ref=None,
                receipt_evidence_fingerprint=None,
                submitted_by=None,
                submitted_at=None,
                approved_by=None,
                approved_at=None,
                approval_reference=None,
                status="APPROVED",
            )
        ],
        "supplier_invoice_lines": [],
        "journal_entries": [],
        "journal_lines": [],
        "posted_ledger_lines": [],
        "journal_line_dimensions": [],
        "posted_ledger_dimensions": [],
    }
    line = source["supplier_invoice_line"][0]
    target["supplier_invoice_lines"] = [
        dict(
            id=line["line_id"],
            tenant_id=T,
            invoice_id=INVOICE,
            line_number=1,
            description=line["description"],
            quantity="3",
            unit_price="35",
            line_amount="100",
            tax_amount="0",
            posting_account_ref=EXP,
            tax_account_ref=None,
            dimension_refs=[],
        )
    ]
    journal = source["journal_entry"][0]
    target["journal_entries"] = [
        dict(
            id=J,
            tenant_id=T,
            number="JE1",
            kind="STANDARD",
            status="POSTED",
            source_owner="legacy-erp",
            source_document_kind="SUPPLIER_INVOICE",
            source_document_id=f"legacy:gl.journal_entry:{J}",
            approval_reference=f"legacy:gl.journal_entry:{J}@{DAY}",
            source_version="candidate",
            source_fingerprint="a" * 64,
            request_fingerprint="b" * 64,
            reverses_journal_id=None,
            reversal_journal_id=None,
            **{
                k: journal[k]
                for k in (
                    "currency_code",
                    "exchange_rate",
                    "entry_date",
                    "posting_date",
                    "fiscal_period_id",
                    "description",
                    "reference",
                    "total_debit",
                    "total_credit",
                    "total_debit_functional",
                    "total_credit_functional",
                    "posted_at",
                )
            },
            posted_by=S,
        )
    ]
    for n, account, debit, credit in ((21, EXP, "100", "0"), (22, AP, "0", "100")):
        lid, pid = str(UUID(int=n)), str(UUID(int=n + 10))
        sl = dict(
            line_id=lid,
            journal_entry_id=J,
            line_number=n - 20,
            account_id=account,
            description=None,
            debit_amount=debit,
            credit_amount=credit,
            debit_amount_functional=debit,
            credit_amount_functional=credit,
            currency_code="NGN",
            exchange_rate="1",
            business_unit_id=None,
            cost_center_id=None,
            project_id=None,
            segment_id=None,
        )
        source["journal_entry_line"].append(sl)
        target["journal_lines"].append(
            dict(
                id=lid,
                tenant_id=T,
                journal_id=J,
                line_number=n - 20,
                account_id=account,
                description=None,
                debit=debit,
                credit=credit,
                debit_functional=debit,
                credit_functional=credit,
            )
        )
        source["posted_ledger_line"].append(
            dict(
                ledger_line_id=pid,
                organization_id=T,
                journal_entry_id=J,
                journal_line_id=lid,
                account_id=account,
                account_code=str(n),
                fiscal_period_id=PERIOD,
                entry_date=DAY,
                posting_date=DAY,
                description=None,
                journal_reference=None,
                debit_amount=debit,
                credit_amount=credit,
                original_currency_code="NGN",
                original_debit_amount=debit,
                original_credit_amount=credit,
                exchange_rate="1",
                source_module="AP",
                source_document_type="SUPPLIER_INVOICE",
                source_document_id=INVOICE,
                posted_by_user_id=S,
                posted_at=STAMP,
                business_unit_id=None,
                cost_center_id=None,
                project_id=None,
                segment_id=None,
            )
        )
        target["posted_ledger_lines"].append(
            dict(
                id=pid,
                tenant_id=T,
                journal_id=J,
                journal_line_id=lid,
                account_id=account,
                account_code=str(n),
                fiscal_period_id=PERIOD,
                journal_number="JE1",
                entry_date=DAY,
                posting_date=DAY,
                description=None,
                debit=debit,
                credit=credit,
                original_debit=debit,
                original_credit=credit,
                currency_code="NGN",
                exchange_rate="1",
                source_owner="legacy-erp",
                source_document_kind="SUPPLIER_INVOICE",
                source_document_id=INVOICE,
                source_version="candidate",
                source_fingerprint="c" * 64,
                posted_by=S,
                posted_at=STAMP,
            )
        )
    for table in ("supplier_invoice", "supplier_invoice_line", "journal_entry", "journal_entry_line"):
        for row in source[table]:
            row["created_at"] = STAMP
    for table in ("supplier_invoices", "supplier_invoice_lines", "journal_entries", "journal_lines"):
        for row in target[table]:
            row["created_at"] = STAMP
    target["supplier_invoices"][0]["request_fingerprint"] = "d" * 64
    return source, target


@pytest.fixture
def db(tmp_path):
    src, dst = facts()
    engines = [sa.create_engine(f"sqlite:///{tmp_path / name}") for name in ("source.db", "destination.db")]
    with engines[0].connect() as source, engines[1].connect() as target:
        target.exec_driver_sql("ATTACH DATABASE ':memory:' AS mod_payables")
        target.exec_driver_sql("ATTACH DATABASE ':memory:' AS mod_accounting")
        tables = {}
        for connection, data in ((source, src), (target, dst)):
            for name, rows in data.items():
                schema = (
                    None
                    if connection is source
                    else "mod_payables"
                    if name.startswith("supplier_")
                    else "mod_accounting"
                )
                sample = (
                    rows[0]
                    if rows
                    else {"invoice_id": INVOICE}
                    if name == "payment_allocation"
                    else dict(
                        id=INVOICE,
                        tenant_id=T,
                        journal_line_id=INVOICE,
                        ledger_line_id=INVOICE,
                        dimension_id=INVOICE,
                        dimension_value_id=INVOICE,
                        dimension_code="",
                        value_code="",
                    )
                )
                table = sa.Table(
                    name,
                    sa.MetaData(),
                    *(
                        sa.Column(
                            k,
                            sa.JSON
                            if isinstance(v, list)
                            else sa.Boolean
                            if isinstance(v, bool)
                            else sa.Integer
                            if isinstance(v, int)
                            else sa.Text,
                        )
                        for k, v in sample.items()
                    ),
                    schema=schema,
                )
                table.create(connection)
                if rows:
                    connection.execute(table.insert(), rows)
                tables[(connection is source, name)] = table
        source.commit()
        target.commit()
        manifest = SnapshotManifest("d" * 64, "source.db", "destination.db", "candidate", (T,))
        verifier = HistoricalPayablesVerifier(source, manifest)
        with Session(bind=target) as session:
            yield source, target, session, verifier, tables
    for engine in engines:
        engine.dispose()


def evidence(db):
    db[0].exec_driver_sql("PRAGMA query_only=1")
    return db[3].evidence(scope=SimpleNamespace(tenant_id=UUID(T)), invoice_id=UUID(INVOICE), destination=db[2])


def test_complete_evidence_preserves_null_and_inclusive_line(db):
    item = evidence(db)
    assert item.total_amount == Decimal("100")
    assert item.supplier_document_number is None
    assert item.approved_by is item.approved_at is item.approval_reference is None
    assert item.fingerprint().startswith("cv1:")
    command = db[3].command(item)
    assert db[3].verify(scope=SimpleNamespace(tenant_id=UUID(T)), command=command, destination=db[2]) == item
    with pytest.raises(HistoricalEvidenceError, match="command_fingerprint"):
        db[3].verify(
            scope=SimpleNamespace(tenant_id=UUID(T)),
            command=replace(command, evidence_fingerprint="cv1:" + "f" * 64),
            destination=db[2],
        )


@pytest.mark.parametrize(
    "is_source,table,column,value,reason",
    [
        (True, "supplier_invoice", "amount_paid", "1", "source_paid"),
        (True, "supplier_invoice", "invoice_type", "CREDIT_NOTE", "source_type"),
        (True, "supplier_invoice", "status", "APPROVED", "source_status"),
        (True, "supplier_invoice", "withholding_tax_amount", "1", "source_withholding"),
        (True, "supplier_invoice", "is_prepayment", True, "source_prepayment"),
        (True, "supplier_invoice", "is_prepayment", None, "source_prepayment"),
        (True, "supplier_invoice", "total_amount", "100.000001", "source_amount_scale"),
        (True, "supplier_invoice", "subtotal", "100.000001", "source_amount_scale"),
        (True, "supplier_invoice", "due_date", None, "source_due_date"),
        (True, "supplier_invoice", "posted_at", None, "source_posted_at"),
        (True, "journal_entry", "source_document_id", S, "source_journal_invoice"),
        (True, "journal_entry", "source_module", "AR", "source_journal_owner"),
        (True, "journal_entry", "is_reversal", True, "source_journal_reversal"),
        (True, "journal_entry", "is_reversal", None, "source_journal_reversal"),
        (True, "organization", "functional_currency_code", "USD", "unsupported_currency"),
        (False, "supplier_invoices", "supplier_document_number", "FAKE", "header_supplier_document_number"),
        (False, "supplier_invoices", "supplier_name_snapshot", "Other", "header_supplier_name_snapshot"),
        (False, "supplier_invoices", "procurement_ref", "legacy-unverified-order", "unsupported_procurement_evidence"),
        (False, "supplier_invoices", "receipt_evidence_fingerprint", "a" * 64, "unsupported_procurement_evidence"),
        (False, "supplier_invoice_lines", "line_amount", "99", "invoice_line_line_amount"),
        (False, "journal_entries", "source_document_id", INVOICE, "journal_source_document_id"),
        (False, "journal_entries", "approval_reference", "fake", "journal_posting_stamp"),
        (
            False,
            "journal_entries",
            "source_document_id",
            f"legacy:public.journal_entry:{J}",
            "journal_source_document_id",
        ),
        (
            False,
            "journal_entries",
            "approval_reference",
            f"legacy:public.journal_entry:{J}@{DAY}",
            "journal_posting_stamp",
        ),
        (False, "journal_lines", "credit", "99", "journal_line_credit"),
        (False, "posted_ledger_lines", "original_credit", "99", "ledger_original_credit"),
    ],
)
def test_near_misses_fail_closed(db, is_source, table, column, value, reason):
    conn = db[0] if is_source else db[1]
    conn.execute(db[4][is_source, table].update().values(**{column: value}))
    with pytest.raises(HistoricalEvidenceError, match=reason):
        evidence(db)


def test_any_allocation_even_zero_is_excluded(db):
    db[0].execute(db[4][True, "payment_allocation"].insert().values(invoice_id=INVOICE))
    with pytest.raises(HistoricalEvidenceError, match="source_allocations"):
        evidence(db)


def test_extra_line_is_not_hidden(db):
    table = db[4][False, "supplier_invoice_lines"]
    row = dict(db[1].execute(sa.select(table)).mappings().one())
    row["id"] = str(UUID(int=999))
    db[1].execute(table.insert().values(**row))
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_count"):
        evidence(db)


def test_readonly_binding_scope_and_select_only(db):
    with pytest.raises(HistoricalEvidenceError, match="source_not_readonly"):
        db[3].evidence(scope=SimpleNamespace(tenant_id=UUID(T)), invoice_id=UUID(INVOICE), destination=db[2])
    evidence(db)
    with pytest.raises(HistoricalEvidenceError, match="tenant_extent"):
        db[3].evidence(scope=SimpleNamespace(tenant_id=UUID(int=999)), invoice_id=UUID(INVOICE), destination=db[2])
    wrong = HistoricalPayablesVerifier(db[0], replace(db[3].manifest, destination_database="wrong"))
    with pytest.raises(HistoricalEvidenceError, match="destination_database"):
        wrong.evidence(scope=SimpleNamespace(tenant_id=UUID(T)), invoice_id=UUID(INVOICE), destination=db[2])
    statements = []

    def record(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().upper())

    sa.event.listen(db[1], "before_cursor_execute", record)
    try:
        evidence(db)
    finally:
        sa.event.remove(db[1], "before_cursor_execute", record)
    assert statements and all(s.startswith(("SELECT", "PRAGMA")) for s in statements)


def test_dirty_session_cannot_flush_as_part_of_verification(db):
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    class Base(DeclarativeBase):
        pass

    class Pending(Base):
        __tablename__ = "must_never_be_written"
        id: Mapped[int] = mapped_column(primary_key=True)

    db[2].add(Pending(id=1))
    with pytest.raises(HistoricalEvidenceError, match="destination_dirty_session"):
        evidence(db)
    assert db[2].new


def test_mutable_owner_status_is_not_part_of_immutable_retry_proof(db):
    original = evidence(db)
    table = db[4][False, "supplier_invoices"]
    db[1].execute(table.update().values(status="VOID"))
    # The owner checks first-admission eligibility after durable replay. It can
    # therefore replay a completed admission despite later settlement/voiding.
    assert evidence(db).fingerprint() == original.fingerprint()


@pytest.mark.parametrize(
    "source_table,column",
    [
        ("supplier_invoice", "organization_id"),
        ("supplier", "organization_id"),
        ("journal_entry", "organization_id"),
        ("posted_ledger_line", "organization_id"),
    ],
)
def test_source_tenant_near_miss(db, source_table, column):
    db[0].execute(db[4][True, source_table].update().values(**{column: str(UUID(int=999))}))
    with pytest.raises(HistoricalEvidenceError, match="tenant"):
        evidence(db)


@pytest.mark.parametrize(
    "table", ["supplier_invoices", "supplier_invoice_lines", "journal_entries", "journal_lines", "posted_ledger_lines"]
)
def test_destination_tenant_near_miss(db, table):
    db[1].execute(db[4][False, table].update().values(tenant_id=str(UUID(int=999))))
    with pytest.raises(HistoricalEvidenceError, match="tenant"):
        evidence(db)


def test_same_command_survives_later_settlement_structures(db):
    original = evidence(db)
    command = db[3].command(original)
    obligation = sa.Table(
        "payment_obligations",
        sa.MetaData(),
        sa.Column("tenant_id", sa.Text),
        sa.Column("invoice_id", sa.Text),
        sa.Column("outstanding_amount", sa.Text),
        schema="mod_payables",
    )
    observation = sa.Table(
        "settlement_observations",
        sa.MetaData(),
        sa.Column("tenant_id", sa.Text),
        sa.Column("invoice_id", sa.Text),
        sa.Column("amount", sa.Text),
        schema="mod_payables",
    )
    obligation.create(db[1])
    observation.create(db[1])
    db[1].execute(obligation.insert().values(tenant_id=T, invoice_id=INVOICE, outstanding_amount="100"))
    db[1].execute(obligation.update().values(outstanding_amount="0"))
    db[1].execute(observation.insert().values(tenant_id=T, invoice_id=INVOICE, amount="100"))
    assert db[3].verify(scope=SimpleNamespace(tenant_id=UUID(T)), command=command, destination=db[2]) == original


def add_dimension(db):
    dimension_id, value_id = str(UUID(int=700)), str(UUID(int=701))
    source, target, _, _, tables = db
    for table_name in ("journal_entry_line", "posted_ledger_line"):
        table = tables[True, table_name]
        source.execute(table.update().where(table.c.account_id == EXP).values(cost_center_id=value_id))
    definitions = sa.Table(
        "accounting_dimensions",
        sa.MetaData(),
        sa.Column("id", sa.Text),
        sa.Column("tenant_id", sa.Text),
        sa.Column("code", sa.Text),
        schema="mod_accounting",
    )
    values = sa.Table(
        "accounting_dimension_values",
        sa.MetaData(),
        sa.Column("id", sa.Text),
        sa.Column("tenant_id", sa.Text),
        sa.Column("dimension_id", sa.Text),
        sa.Column("code", sa.Text),
        schema="mod_accounting",
    )
    definitions.create(target)
    values.create(target)
    target.execute(definitions.insert().values(id=dimension_id, tenant_id=T, code="COST_CENTER"))
    target.execute(values.insert().values(id=value_id, tenant_id=T, dimension_id=dimension_id, code="CC1"))
    for table_name, parent in (
        ("journal_line_dimensions", str(UUID(int=21))),
        ("posted_ledger_dimensions", str(UUID(int=31))),
    ):
        target.execute(
            tables[False, table_name]
            .insert()
            .values(
                id=str(UUID(int=800 if table_name == "journal_line_dimensions" else 801)),
                tenant_id=T,
                journal_line_id=parent,
                ledger_line_id=parent,
                dimension_id=dimension_id,
                dimension_value_id=value_id,
                dimension_code="COST_CENTER",
                value_code="CC1",
            )
        )
    return definitions, values


def test_preserved_accounting_dimension_and_invoice_dimension(db):
    add_dimension(db)
    source_line = db[4][True, "supplier_invoice_line"]
    target_line = db[4][False, "supplier_invoice_lines"]
    value_id = str(UUID(int=701))
    db[0].execute(source_line.update().values(cost_center_id=value_id))
    db[1].execute(target_line.update().values(dimension_refs=[["COST_CENTER", value_id]]))
    assert evidence(db).accounting_evidence_fingerprint.startswith("cv1:")


@pytest.mark.parametrize(
    "table,column,value,reason",
    [
        ("definition", "tenant_id", str(UUID(int=999)), "accounting_dimension_tenant"),
        ("value", "dimension_id", str(UUID(int=999)), "accounting_dimension_duplicate_or_parent"),
        ("ledger", "value_code", "OTHER", "ledger_dimension_snapshot"),
    ],
)
def test_accounting_dimension_near_miss(db, table, column, value, reason):
    definitions, values = add_dimension(db)
    selected = {"definition": definitions, "value": values, "ledger": db[4][False, "posted_ledger_dimensions"]}[table]
    db[1].execute(selected.update().values(**{column: value}))
    with pytest.raises(HistoricalEvidenceError, match=reason):
        evidence(db)


def test_v1_same_command_survives_unrelated_nullable_destination_columns(db):
    original = evidence(db)
    command = db[3].command(original)
    for table in (
        "mod_payables.supplier_invoices",
        "mod_payables.supplier_invoice_lines",
        "mod_accounting.journal_entries",
        "mod_accounting.journal_lines",
        "mod_accounting.posted_ledger_lines",
    ):
        db[1].exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN future_annotation TEXT")
        db[1].exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN future_numeric_annotation FLOAT")
    assert db[3].verify(scope=SimpleNamespace(tenant_id=UUID(T)), command=command, destination=db[2]) == original


def test_consumed_financial_float_metadata_is_still_refused(db, monkeypatch):
    from erp_v2.importer import payables_history as module

    reflect = module._reflect

    def approximate_money(connection, name, schema=None):
        table = reflect(connection, name, schema)
        if connection is db[0] and name == "supplier_invoice":
            table.c.total_amount.type = sa.Float()
        return table

    monkeypatch.setattr(module, "_reflect", approximate_money)
    with pytest.raises(HistoricalEvidenceError, match="unsupported_float_numeric"):
        evidence(db)
