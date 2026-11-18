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
            "invoice_type text, ap_control_account_id uuid, subtotal numeric, total_amount numeric)",
            "CREATE TABLE supplier_invoice_line (line_id uuid, invoice_id uuid, line_amount numeric, "
            "tax_amount numeric, expense_account_id uuid, tax_code_id uuid)",
            "CREATE TABLE tax_code (tax_code_id uuid, organization_id uuid, tax_type text, tax_paid_account_id uuid)",
            "CREATE TABLE account (account_id uuid, deferral_pair_account_id uuid)",
            "CREATE TABLE posted_ledger_line (journal_entry_id uuid, account_id uuid, debit_amount numeric, "
            "credit_amount numeric)",
            "CREATE TABLE journal_entry (journal_entry_id uuid, source_module text, source_document_type text, "
            "source_document_id uuid, status text)",
        ):
            c.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
            c.exec_driver_sql(stmt)
        c.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
        c.execute(sa.text("INSERT INTO tax_code VALUES (:t, :o, 'VAT', :p)"), dict(t=TAX_CODE, o=ORG, p=PAID))
        c.execute(sa.text("INSERT INTO account VALUES (:p, NULL)"), dict(p=PAID))
        c.execute(
            sa.text(
                "INSERT INTO supplier_invoice VALUES (:i, :o, NULL, 'INVOICE', NULL, NULL, NULL), "
                "(:n, :o, NULL, 'CREDIT_NOTE', NULL, NULL, NULL)"
            ),
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


DEFERRAL = "SUPPLIER_INVOICE_VAT_DEFERRAL"
DEFER_ACCT, CTRL = str(UUID(int=21)), str(UUID(int=50))


def _deferral_schema(engine, *, deferral_debits, status="POSTED", subtotal=100, total=110, posted=True, amount=110):
    """One taxed legacy-posted invoice (folded: Dr expense 110 / Cr 2000 110) and its deferral journal(s).

    ``deferral_debits`` is a list of the deferral journals' 1455 debit amounts (empty: no deferral journal).
    Each deferral journal is Dr 1455 amount / Cr expense amount.
    """

    schema = f"legacy_{uuid4().hex[:12]}"
    journal = str(UUID(int=9100))
    with engine.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
        for stmt in (
            "CREATE TABLE supplier_invoice (invoice_id uuid, organization_id uuid, journal_entry_id uuid, "
            "invoice_type text, ap_control_account_id uuid, subtotal numeric, total_amount numeric)",
            "CREATE TABLE supplier_invoice_line (line_id uuid, invoice_id uuid, line_amount numeric, "
            "tax_amount numeric, expense_account_id uuid, tax_code_id uuid)",
            "CREATE TABLE tax_code (tax_code_id uuid, organization_id uuid, tax_type text, tax_paid_account_id uuid)",
            "CREATE TABLE account (account_id uuid, deferral_pair_account_id uuid)",
            "CREATE TABLE posted_ledger_line (journal_entry_id uuid, account_id uuid, debit_amount numeric, "
            "credit_amount numeric)",
            "CREATE TABLE journal_entry (journal_entry_id uuid, source_module text, source_document_type text, "
            "source_document_id uuid, status text)",
        ):
            c.exec_driver_sql(stmt)
        c.execute(sa.text("INSERT INTO tax_code VALUES (:t, :o, 'VAT', :p)"), dict(t=TAX_CODE, o=ORG, p=PAID))
        c.execute(sa.text("INSERT INTO account VALUES (:p, :d), (:d, NULL)"), dict(p=PAID, d=DEFER_ACCT))
        c.execute(
            sa.text("INSERT INTO supplier_invoice VALUES (:i, :o, :j, 'INVOICE', :c, :sub, :tot)"),
            dict(i=INVOICE, o=ORG, j=journal if posted else None, c=CTRL, sub=subtotal, tot=total),
        )
        c.execute(
            sa.text("INSERT INTO supplier_invoice_line VALUES (:l, :i, 100, 10, :e, :t)"),
            dict(l=str(UUID(int=2001)), i=INVOICE, e=EXP, t=TAX_CODE),
        )
        c.execute(sa.text("INSERT INTO posted_ledger_line VALUES (:j, :e, :a, NULL)"), dict(j=journal, e=EXP, a=amount))
        c.execute(
            sa.text("INSERT INTO posted_ledger_line VALUES (:j, :c, NULL, :a)"), dict(j=journal, c=CTRL, a=amount)
        )
        for n, amount in enumerate(deferral_debits):
            dj = str(UUID(int=8000 + n))
            c.execute(
                sa.text("INSERT INTO journal_entry VALUES (:j, 'AP', :t, :i, :s)"),
                dict(j=dj, t=DEFERRAL, i=INVOICE, s=status),
            )
            c.execute(
                sa.text("INSERT INTO posted_ledger_line VALUES (:j, :d, :a, NULL), (:j, :e, NULL, :a)"),
                dict(j=dj, d=DEFER_ACCT, e=EXP, a=amount),
            )
    return schema


def _run(engine, **kwargs):
    schema = _deferral_schema(engine, **kwargs)
    try:
        with engine.connect() as source:
            source.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
            maps = EnrolmentMaps(source, source)
            built = maps.get("supplier_line_tax_accounts")
            counts = maps.tax_account_classes()
            source.rollback()
    finally:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
    return built[UUID(int=2001).hex]["tax_account_ref"], counts


def test_deferral_journal_equal_to_the_vat_is_a1d_on_the_split_account(engine):
    ref, counts = _run(engine, deferral_debits=["10"])
    assert ref == DEFER_ACCT
    assert counts["A1D"] == dict(lines=1, invoices=1) and counts["A3"] == dict(lines=0, invoices=0)
    assert counts["a1d_expense_elsewhere"] == dict(lines=0, invoices=0)


def test_unequal_deferral_journal_is_a3_deferral_amount_mismatch(engine):
    ref, counts = _run(engine, deferral_debits=["9"])
    assert ref == EXP and counts["A1D"]["invoices"] == 0
    assert counts["a3_reasons"]["deferral_amount_mismatch"] == dict(lines=1, invoices=1)


def test_missing_deferral_journal_falls_back_to_a2(engine):
    ref, counts = _run(engine, deferral_debits=[])
    assert ref == EXP and counts["A2"] == dict(lines=1, invoices=1) and counts["A1D"]["invoices"] == 0


def test_multiple_deferral_journals_are_a3_and_never_summed(engine):
    ref, counts = _run(engine, deferral_debits=["5", "5"])
    assert ref == EXP and counts["A1D"]["invoices"] == 0
    assert counts["a3_reasons"]["deferral_journal_multiple"] == dict(lines=1, invoices=1)


def test_not_posted_deferral_journal_is_ignored_and_counted(engine):
    _ref, counts = _run(engine, deferral_debits=["10"], status="DRAFT")
    assert counts["deferral_journal_not_posted"] == dict(lines=0, invoices=1)
    assert counts["A1D"]["invoices"] == 0 and counts["A2"]["invoices"] == 1


def test_lines_exceeding_the_header_are_a3_with_their_own_reason(engine):
    # Lines 100 + 10, header 90 / 100, legacy posted 100 and a deferral journal of 10: a header/line fault.
    ref, counts = _run(engine, deferral_debits=["10"], subtotal=90, total=100, amount=100)
    assert ref == EXP and counts["A1D"]["invoices"] == 0
    assert counts["a3_reasons"]["invoice_lines_exceed_header"] == dict(lines=1, invoices=1)
    assert counts["a3_reasons"]["deferral_combined_unbalanced"] == dict(lines=0, invoices=0)


def test_unposted_invoice_with_a_posted_deferral_journal_is_counted_and_keeps_its_b1_ref(engine):
    ref, counts = _run(engine, deferral_debits=["10"], posted=False)
    assert ref == DEFER_ACCT  # the map still lands the B1 ref; the verifier path refuses the invoice
    assert counts["deferral_on_unposted_invoice"] == dict(lines=1, invoices=1)
    assert counts["B1"] == dict(lines=1, invoices=1) and counts["A1D"]["invoices"] == 0
