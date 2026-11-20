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
            "total_amount numeric, currency_code varchar(3));"
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
    body, report = vx.derive(uuid4(), carried, data)
    item = body["items"][0]
    assert item["reclass"] == ["10.000000"] and item["journal_1455_credit"] == "20.000000"
    assert report["tie"]["gl_net_credit"] == "74.000000" and report["tie"]["other_net"] == "57.000000"
    assert report["items_loadable"] == 1
    with engine.connect() as conn:
        rerun = vx.read_legacy(conn, org, date(2026, 2, 1), [str(invoice)])
    again = vx.derive(uuid4(), carried, rerun)
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
