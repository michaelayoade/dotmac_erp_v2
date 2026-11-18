"""B-3 transforms against a real PostgreSQL legacy-shaped snapshot (hosted CI only: ERP_TEST_POSTGRES_URL via the
shared fixture): the NULL-expense fallback lines, the D-M2 WHT removal and the bridge partition, through
``EnrolmentMaps`` exactly as the importer reads the source (legacy names via the search_path)."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.importer.enrolment import EnrolmentMaps
from erp_v2.importer.transforms import TransformParams

D = Decimal


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


def u(n):
    return str(UUID(int=n))


ORG, EXP, PAID, VAT_CODE, WHT_CODE = u(1), u(10), u(20), u(77), u(78)
TENANT = u(7)
INPUT_TAX, FALLBACK = u(21), u(40)
INV_NULL, INV_WHT, INV_QUEUE = u(1001), u(1002), u(1003)


def _snapshot(engine):
    schema = f"legacy_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
        for stmt in (
            "CREATE TABLE supplier_invoice (invoice_id uuid, organization_id uuid, journal_entry_id uuid, "
            "ap_control_account_id uuid, status text, subtotal numeric, tax_amount numeric, total_amount numeric)",
            "CREATE TABLE supplier_invoice_line (line_id uuid, invoice_id uuid, line_amount numeric, "
            "tax_amount numeric, expense_account_id uuid, tax_code_id uuid, item_id uuid)",
            "CREATE TABLE tax_code (tax_code_id uuid, organization_id uuid, tax_type text, tax_paid_account_id uuid)",
            "CREATE TABLE account (account_id uuid, account_code text, deferral_pair_account_id uuid, "
            "is_active boolean, is_posting_allowed boolean)",
            "CREATE TABLE posted_ledger_line (journal_entry_id uuid, account_id uuid, debit_amount numeric, "
            "credit_amount numeric)",
            "CREATE TABLE journal_entry (journal_entry_id uuid, source_module text, source_document_type text, "
            "source_document_id uuid, status text)",
            # stands in for the destination chart (mod_accounting accounts joined to their category)
            "CREATE TABLE dest_chart (id uuid, tenant_id uuid, code text, is_active boolean, "
            "posting_allowed boolean, account_class text)",
        ):
            c.exec_driver_sql(stmt)
        c.execute(
            sa.text(
                "INSERT INTO account VALUES (:e,'5000',NULL,true,true), (:p,'1440',:t,true,true), "
                "(:t,'1455',NULL,true,true), (:f,'5999',NULL,true,true), (:c,'2000',NULL,true,true)"
            ),
            dict(e=EXP, p=PAID, t=INPUT_TAX, f=FALLBACK, c=u(50)),
        )
        c.execute(
            sa.text(
                "INSERT INTO dest_chart VALUES (:e,:t,'5000',true,true,'EXPENSE'), (:f,:t,'5999',true,true,'EXPENSE'), "
                "(:c,:t,'2000',true,true,'LIABILITY')"
            ),
            dict(e=EXP, f=FALLBACK, c=u(50), t=TENANT),
        )
        c.execute(
            sa.text("INSERT INTO tax_code VALUES (:v, :o, 'VAT', :p), (:w, :o, 'WITHHOLDING', :p)"),
            dict(v=VAT_CODE, w=WHT_CODE, o=ORG, p=PAID),
        )
        c.execute(
            sa.text(
                "INSERT INTO supplier_invoice VALUES "
                "(:a, :o, NULL, NULL, 'SUBMITTED', 90, 0, 90), "
                "(:b, :o, NULL, NULL, 'SUBMITTED', 300, 17.5, 317.5), "
                "(:c, :o, NULL, NULL, 'DRAFT', 70, 0, 70)"
            ),
            dict(a=INV_NULL, b=INV_WHT, c=INV_QUEUE, o=ORG),
        )
        lines = [
            (u(2001), INV_NULL, 30, 0, None, None),
            (u(2002), INV_NULL, 30, 0, None, None),
            (u(2003), INV_NULL, 30, 0, EXP, None),
            (u(2004), INV_WHT, 100, 7.5, EXP, VAT_CODE),
            (u(2005), INV_WHT, 200, 10, EXP, WHT_CODE),
            (u(2006), INV_QUEUE, 70, 0, u(50), None),  # a control account as the expense account
        ]
        for line_id, inv, amount, tax, expense, code in lines:
            c.execute(
                sa.text("INSERT INTO supplier_invoice_line VALUES (:l, :i, :a, :t, :e, :c, NULL)"),
                dict(l=line_id, i=inv, a=amount, t=tax, e=expense, c=code),
            )
    return schema


def test_null_expense_lines_land_wht_is_removed_and_the_partition_balances(engine, monkeypatch):
    from erp_v2.importer import transforms

    monkeypatch.setattr(
        transforms,
        "DEST_ACCOUNT_SQL",
        "SELECT id, tenant_id, code, is_active, posting_allowed, account_class FROM dest_chart",
    )
    schema = _snapshot(engine)
    try:
        with engine.connect() as source:
            source.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
            maps = EnrolmentMaps(source, source, transform_params=TransformParams(FALLBACK, TENANT))
            lines = maps.get("supplier_line_transforms")
            headers = maps.get("supplier_invoice_transforms")
            ledger = maps.transform_ledger()
            source.rollback()
    finally:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
    key = lambda n: UUID(int=n).hex  # noqa: E731
    # the 2 NULL-expense lines land on the fallback; the third keeps its own account
    assert [lines[key(n)]["posting_account_ref"] for n in (2001, 2002, 2003)] == [FALLBACK, FALLBACK, EXP]
    # D-M2: VAT line on 1455, WHT line stripped, total = gross
    assert lines[key(2004)]["tax_account_ref"] == INPUT_TAX
    assert lines[key(2005)]["tax_amount"] == D(0) and lines[key(2005)]["tax_account_ref"] is None
    assert (headers[key(1002)]["tax_amount"], headers[key(1002)]["total_amount"]) == (D("7.5"), D("307.5"))
    # the control-account expense line is queued: line and header carry a named refusal
    assert lines[key(2006)] == {"_refusal": "expense_account_is_control"}
    assert headers[key(1003)] == {"_refusal": "expense_account_is_control"}
    part = ledger.partition()
    assert part["balanced"] is True and part["legacy"] == {"invoices": 3, "lines": 6, "total": "477.5"}
    assert part["buckets"]["QUEUE"]["invoices"] == 2 and part["buckets"]["TRANSFORM"]["invoices"] == 1
    assert part["universe"] == {
        "source_invoices": 3,
        "source_lines": 6,
        "each_invoice_in_exactly_one_bucket": True,
        "line_count_matches_source": True,
    }
    # the WHT invoice lands at the gross but is queued until the a6 WHT table carries the fact
    assert ledger.queue()["by_reason"] == {"expense_account_is_control": 1, "wht_payment_fact_pending": 1}
    # parity: +2 NULL-expense lines land, -1 line the queued invoice no longer lands (dest6 landed it); -1 header
    parity = ledger.parity()
    assert (parity["declared_written_delta_lines"], parity["declared_written_delta_headers"]) == (1, -1)
    assert parity["changed_header_values"] == 1
