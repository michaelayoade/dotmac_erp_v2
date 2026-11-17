"""A non-landing legacy credit note (untaxed negative line) must not abort the tax-account map, on
real PostgreSQL (hosted CI only: ERP_TEST_POSTGRES_URL via the shared fixture)."""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.importer.enrolment import EnrolmentMaps


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


ORG, EXP, PAID = (str(UUID(int=n)) for n in (1, 10, 20))
TAX_CODE, INVOICE, CREDIT_NOTE = (str(UUID(int=n)) for n in (77, 1001, 1002))


def test_untaxed_negative_credit_note_does_not_abort_the_map_and_x_is_zero(engine):
    schema = f"legacy_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        for stmt in (
            "CREATE TABLE supplier_invoice (invoice_id uuid, organization_id uuid, journal_entry_id uuid, "
            "invoice_type text)",
            "CREATE TABLE supplier_invoice_line (line_id uuid, invoice_id uuid, line_amount numeric, "
            "tax_amount numeric, expense_account_id uuid, tax_code_id uuid)",
            "CREATE TABLE tax_code (tax_code_id uuid, organization_id uuid, tax_type text, tax_paid_account_id uuid)",
            "CREATE TABLE account (account_id uuid, deferral_pair_account_id uuid)",
            "CREATE TABLE posted_ledger_line (journal_entry_id uuid, account_id uuid, debit_amount numeric)",
        ):
            c.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
            c.exec_driver_sql(stmt)
        c.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
        c.execute(sa.text("INSERT INTO tax_code VALUES (:t, :o, 'VAT', :p)"), dict(t=TAX_CODE, o=ORG, p=PAID))
        c.execute(sa.text("INSERT INTO account VALUES (:p, NULL)"), dict(p=PAID))
        c.execute(
            sa.text("INSERT INTO supplier_invoice VALUES (:i, :o, NULL, 'INVOICE'), (:n, :o, NULL, 'CREDIT_NOTE')"),
            dict(i=INVOICE, n=CREDIT_NOTE, o=ORG),
        )
        c.execute(
            sa.text("INSERT INTO supplier_invoice_line VALUES (:l1, :i, 100, 10, :e, :t), (:l2, :n, -40, 0, :e, NULL)"),
            dict(l1=str(UUID(int=2001)), l2=str(UUID(int=2002)), i=INVOICE, n=CREDIT_NOTE, e=EXP, t=TAX_CODE),
        )
    with engine.connect() as source:
        source.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
        maps = EnrolmentMaps(source, source)
        built = maps.get("supplier_line_tax_accounts")
        counts = maps.tax_account_classes()
        source.rollback()
    with engine.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
    assert built[UUID(int=2001).hex]["tax_account_ref"] == PAID
    assert built[UUID(int=2002).hex]["tax_account_ref"] is None
    assert counts["X"] == dict(lines=0, invoices=0)
    assert counts["B1"] == dict(lines=1, invoices=1)
