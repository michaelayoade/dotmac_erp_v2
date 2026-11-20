"""Q1 legacy VAT extraction on PostgreSQL: the legacy reader's SQL against a mini legacy schema that carries the exact
column names verified (metadata only) on the rehearsal snapshot ``erp_legacy_fresh``, and the destination reader's
refusals. Hosted CI only (ERP_TEST_POSTGRES_URL)."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.billing_gl import vat_extract as vx
from erp_v2.migration_ledger.common import LedgerRefused

D = Decimal


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


@pytest.fixture
def legacy(engine):
    """A throwaway legacy schema: ar / gl / tax with the columns the extractor reads."""
    with engine.begin() as conn:
        for schema in ("ar", "gl", "tax"):
            assert not conn.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :s)"), {"s": schema}
            )
            conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
        conn.exec_driver_sql(
            "CREATE TABLE gl.account (account_id uuid PRIMARY KEY, organization_id uuid, "
            "deferral_pair_account_id uuid);"
            "CREATE TABLE tax.tax_code (tax_code_id uuid PRIMARY KEY, organization_id uuid, tax_code varchar(30), "
            "tax_type varchar(10), tax_collected_account_id uuid);"
            "CREATE TABLE gl.journal_entry (journal_entry_id uuid PRIMARY KEY, organization_id uuid, "
            "currency_code varchar(3), is_reversal boolean, reversed_journal_id uuid);"
            "CREATE TABLE gl.posted_ledger_line (line_id uuid PRIMARY KEY, organization_id uuid, "
            "journal_entry_id uuid, "
            "account_id uuid, posting_date date, debit_amount numeric, credit_amount numeric, source_module varchar, "
            "source_document_type varchar, source_document_id uuid);"
            "CREATE TABLE ar.invoice (invoice_id uuid PRIMARY KEY, organization_id uuid, invoice_number varchar, "
            "total_amount numeric, currency_code varchar(3), invoice_type varchar DEFAULT 'STANDARD');"
            "CREATE TABLE ar.invoice_line (line_id uuid PRIMARY KEY, invoice_id uuid, line_number int, "
            "tax_code_id uuid, "
            "tax_amount numeric);"
            "CREATE TABLE ar.invoice_line_tax (line_tax_id uuid PRIMARY KEY, line_id uuid, tax_code_id uuid, "
            "tax_amount numeric, sequence int);"
            "CREATE TABLE ar.payment_allocation (allocation_id uuid PRIMARY KEY, payment_id uuid, invoice_id uuid, "
            "allocated_amount numeric)"
        )
    yield
    with engine.begin() as conn:
        for schema in ("ar", "gl", "tax"):
            conn.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")


def test_the_legacy_reader_and_derivation_end_to_end(engine, legacy):
    org = uuid4()
    collected, deferral = uuid4(), uuid4()
    tax_id, invoice, payment, other_inv = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as conn:
        x = conn.execute
        x(
            sa.text("INSERT INTO gl.account VALUES (:a, :o, :d), (:d, :o, NULL)"),
            {"a": collected, "o": org, "d": deferral},
        )
        x(
            sa.text("INSERT INTO tax.tax_code VALUES (:t, :o, 'VAT7.5', 'VAT', :a)"),
            {"t": tax_id, "o": org, "a": collected},
        )
        x(
            sa.text("INSERT INTO ar.invoice VALUES (:i, :o, 'INV-1', 120.00, 'NGN'), (:j, :o, 'INV-2', 70.00, 'NGN')"),
            {"i": invoice, "j": other_inv, "o": org},
        )
        for inv_id, tax in ((invoice, "20.00"), (other_inv, "7.00")):
            line_id = uuid4()
            x(
                sa.text("INSERT INTO ar.invoice_line VALUES (:l, :i, 1, :t, :x)"),
                {"l": line_id, "i": inv_id, "t": tax_id, "x": tax},
            )
            x(
                sa.text("INSERT INTO ar.invoice_line_tax VALUES (gen_random_uuid(), :l, :t, :x, 1)"),
                {"l": line_id, "t": tax_id, "x": tax},
            )
        x(
            sa.text("INSERT INTO ar.payment_allocation VALUES (gen_random_uuid(), :p, :i, 60.00)"),
            {"p": payment, "i": invoice},
        )

        def gl(source, doc, debit, credit, posting, module="AR", currency="NGN"):
            journal = uuid4()
            x(
                sa.text("INSERT INTO gl.journal_entry VALUES (:j, :o, :c, false, NULL)"),
                {"j": journal, "o": org, "c": currency},
            )
            x(
                sa.text(
                    "INSERT INTO gl.posted_ledger_line VALUES "
                    "(gen_random_uuid(), :o, :j, :a, :d, :db, :cr, :m, :s, :doc)"
                ),
                {
                    "o": org,
                    "j": journal,
                    "a": deferral,
                    "d": posting,
                    "db": debit,
                    "cr": credit,
                    "m": module,
                    "s": source,
                    "doc": doc,
                },
            )

        gl("INVOICE", invoice, 0, 20, date(2026, 1, 5))
        gl("AR_INVOICE_VAT_DEFERRAL", other_inv, 0, 7, date(2026, 1, 6))
        gl("CUSTOMER_PAYMENT_VAT_RECLASS", payment, 10, 0, date(2026, 1, 8))
        gl("RECLASS", None, 0, 57, date(2026, 1, 9), module="GL")
        gl("INVOICE", other_inv, 0, 99, date(2026, 3, 1))  # after the cutoff: not read
    with engine.connect() as conn:
        data = vx.read_legacy(conn, org, date(2026, 2, 1), [str(invoice)])
    assert {g.source_type for g in data.gl} == {
        "INVOICE",
        "AR_INVOICE_VAT_DEFERRAL",
        "CUSTOMER_PAYMENT_VAT_RECLASS",
        "RECLASS",
    }
    assert data.tax[str(tax_id)].deferrable and set(data.invoices) == {str(invoice), str(other_inv)}
    carried = [vx.Carried(str(invoice), uuid4(), D("60.00"), "NGN")]
    body, report = vx.derive(uuid4(), carried, data, v2_opening_net_credit=D("74"), ap_positions=D(0))
    item = body["items"][0]
    assert item["reclass"] == ["10.000000"] and item["journal_1455_credit"] == "20.000000"
    assert report["tie"]["gl_net_credit"] == "74.000000" and report["tie"]["other_net"] == "57.000000"
    assert data.fingerprint["ar_invoice"]["count"] == 2 and data.credit_note_ids == frozenset()
    assert report["items_loadable"] == 1
    with engine.connect() as conn:
        rerun = vx.read_legacy(conn, org, date(2026, 2, 1), [str(invoice)])
    again = vx.derive(uuid4(), carried, rerun, v2_opening_net_credit=D("74"), ap_positions=D(0))
    assert vx.seal({**again[0], "tenant_id": body["tenant_id"]}) == vx.seal(body)  # deterministic re-run


def test_the_destination_reader_refuses_without_admissions_or_on_a_different_snapshot(engine):
    tenant = uuid4()
    with Session(engine) as db:
        assert db.scalar(sa.text("SELECT to_regclass('public.erp_ar_opening_admissions') IS NOT NULL")) is False
        with pytest.raises(LedgerRefused) as unavailable:
            vx.read_carried(db, tenant, "a" * 64)
    assert unavailable.value.code == "ar_admissions_unavailable"
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE public.erp_ar_opening_admissions "
            "(tenant_id uuid, billing_receipt_id uuid, snapshot_sha256 text)"
        )
        conn.execute(
            sa.text("INSERT INTO public.erp_ar_opening_admissions VALUES (:t, gen_random_uuid(), :s)"),
            {"t": tenant, "s": "b" * 64},
        )
    try:
        with Session(engine) as db:
            with pytest.raises(LedgerRefused) as mismatch:
                vx.read_carried(db, tenant, "a" * 64)
            assert mismatch.value.code == "snapshot_mismatch"
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE public.erp_ar_opening_admissions")


def test_the_v2_readers_return_zero_on_an_empty_estate(engine):
    with Session(engine) as db:
        assert vx.read_v2_opening(db, uuid4(), [str(uuid4())]) == D(0)
        assert vx.read_ap_positions(db, uuid4(), [str(uuid4())]) == D(0)  # payables a6 positions are not installed here


def test_read_v2_opening_sums_only_the_opening_owners_on_the_named_accounts(engine):
    from datetime import UTC, datetime

    from dotmac_accounting import service as accounting
    from dotmac_accounting.contracts import CreateJournal, JournalKind, JournalLineInput, SourceIdentity
    from dotmac_kernel.cache import TenantScope
    from test_journals_pg import seed

    env = seed(engine, currency=None)
    scope, now = TenantScope(env.tenant_id), datetime(2026, 1, 15, tzinfo=UTC)
    deferral, other = env.accounts["2000"], env.accounts["2040"]

    def post(owner, document, credit_account, amount):
        with Session(engine) as db:
            journal = accounting.create_journal(
                db,
                scope=scope,
                command=CreateJournal(
                    number=f"T-{document}",
                    kind=JournalKind.STANDARD,
                    entry_date=date(2026, 1, 10),
                    posting_date=date(2026, 1, 10),
                    currency_code="NGN",
                    exchange_rate=D(1),
                    description="test",
                    reference=document,
                    source=SourceIdentity(owner, "opening_balance", document, "1", "a" * 64),
                    lines=(
                        JournalLineInput(env.accounts["1000"], D(amount), D(0), None),
                        JournalLineInput(credit_account, D(0), D(amount), None),
                    ),
                ),
                idempotency_key=f"{document}:create",
                idempotency_expires_at=None,
                recorded_at=now,
            )
            accounting.post_journal(
                db,
                scope=scope,
                journal_id=journal.id,
                approval_reference="t",
                posted_by="t",
                idempotency_key=f"{document}:post",
                idempotency_expires_at=None,
                posted_at=now,
            )
            db.commit()

    post("erp.opening", "o1", deferral, "150.00")
    post("erp.ias8", "i1", deferral, "10.00")
    post("billing", "b1", deferral, "999.00")  # not an opening owner: ignored
    post("erp.opening", "o2", other, "77.00")  # an account not named: ignored
    with Session(engine) as db:
        assert vx.read_v2_opening(db, env.tenant_id, [str(deferral)]) == D("160.00")
        assert vx.read_v2_opening(db, env.tenant_id, [str(deferral), str(other)]) == D("237.00")
        assert vx.read_v2_opening(db, uuid4(), [str(deferral)]) == D(0)  # another tenant sees nothing


def test_read_ap_positions_filters_the_tenant_explicitly(engine):
    tenant, other, ref = uuid4(), uuid4(), str(uuid4())
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE mod_payables.historical_tax_deferrals "
            "(tenant_id uuid, deferral_account_ref text, remaining numeric)"
        )
        for t, amount in ((tenant, "5.00"), (tenant, "2.50"), (other, "100.00")):
            conn.execute(
                sa.text("INSERT INTO mod_payables.historical_tax_deferrals VALUES (:t, :r, :a)"),
                {"t": t, "r": ref, "a": amount},
            )
    try:
        with Session(engine) as db:
            assert vx.read_ap_positions(db, tenant, [ref]) == D("7.50")  # the other tenant's 100.00 is never summed
            assert vx.read_ap_positions(db, other, [ref]) == D("100.00")
            assert vx.read_ap_positions(db, tenant, [str(uuid4())]) == D(0)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE mod_payables.historical_tax_deferrals")
