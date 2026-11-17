"""tax_account_ref derivation: the two-regime rule, the enrolment map and its receipt counts."""

from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa

from erp_v2.importer.enrolment import MAP_BUILDERS, EnrolmentMaps
from erp_v2.importer.tax_accounts import (
    LineFacts,
    TaxAccountMappingError,
    TaxCodeFacts,
    class_counts,
    classify_invoice,
)
from erp_v2.importer.waves36 import ALL_WAVE_PAIRINGS

ORG, OTHER_ORG = str(UUID(int=1)), str(UUID(int=2))
EXP, EXP2, PAID, DEFER, TAXACCT, ELSEWHERE, OTHER_VAT = (str(UUID(int=n)) for n in (10, 11, 20, 21, 22, 30, 31))
D = Decimal


def code(*, org=ORG, tax_type="VAT", paid=PAID, deferral=DEFER):
    return TaxCodeFacts(org, tax_type, paid, deferral)


def line(n, tax="10", amount="100", expense=EXP, tax_code=None):
    return LineFacts(str(UUID(int=100 + n)), D(amount), D(tax), expense, tax_code)


def ref(outcome, n):
    item = outcome.lines[str(UUID(int=100 + n))]
    return item.klass, item.ref


# ---- B: not legacy-posted (snapshot rule) ----


@pytest.mark.parametrize(
    "facts,expected",
    [
        (code(), ("B1", DEFER)),  # D4: the deferral pair of the paid account
        (code(deferral=None), ("B1", PAID)),
        (code(tax_type="GST"), ("B1", DEFER)),
        (code(tax_type="WITHHOLDING"), ("B2", EXP)),  # the WHT line folds (reviewed, never posted by B1b)
        (code(org=OTHER_ORG), ("B2", EXP)),
        (code(paid=None), ("B2", EXP)),  # VAT without a tax-paid account
        (None, ("B2", EXP)),  # no code
    ],
)
def test_unposted_invoice_follows_the_snapshot_rule(facts, expected):
    outcome = classify_invoice(ORG, [line(1, tax_code=facts)], None)
    assert ref(outcome, 1) == expected and outcome.klass == "B" and not outcome.legacy_posted


def test_zero_tax_has_no_account_whatever_the_regime():
    for ledger in (None, {EXP: D(100)}):
        outcome = classify_invoice(ORG, [line(1, tax="0", tax_code=code())], ledger)
        assert ref(outcome, 1) == (None, None) and outcome.klass is None


# ---- A: legacy-posted (GL as posted) ----


def test_a1_split_on_the_gl_maps_the_snapshot_deferral_account():
    lines = [line(1, tax_code=code()), line(2, tax="5", amount="50", expense=EXP2, tax_code=code())]
    outcome = classify_invoice(ORG, lines, {EXP: D(100), EXP2: D(50), DEFER: D(15)})
    assert outcome.klass == "A1" and not outcome.other
    assert ref(outcome, 1) == ("A1", DEFER) and ref(outcome, 2) == ("A1", DEFER)


def test_a1_does_not_prove_where_the_expense_was_posted_but_counts_it():
    outcome = classify_invoice(ORG, [line(1, tax_code=code())], {ELSEWHERE: D(100), DEFER: D(10)})
    assert outcome.klass == "A1" and outcome.expense_elsewhere
    assert class_counts([outcome])["a1_expense_elsewhere"] == dict(lines=1, invoices=1)
    on_lines = classify_invoice(ORG, [line(1, tax_code=code())], {EXP: D(100), DEFER: D(10)})
    assert on_lines.klass == "A1" and not on_lines.expense_elsewhere


def test_debit_on_another_codes_tax_account_is_never_a1_or_a2():
    """The set is global: another code's VAT account, offset by less expense, must not read as A1."""
    ledger = {EXP: D(90), DEFER: D(10), OTHER_VAT: D(10)}  # totals the invoice (110)
    own_codes_only = classify_invoice(ORG, [line(1, tax_code=code())], ledger)
    assert own_codes_only.klass == "A1"  # what a per-invoice code set would wrongly accept
    outcome = classify_invoice(ORG, [line(1, tax_code=code())], ledger, [OTHER_VAT, None])
    assert outcome.klass == "A3" and ref(outcome, 1) == ("A3", EXP)
    folded_with_vat_debit = classify_invoice(
        ORG, [line(1, tax_code=code())], {EXP: D(100), OTHER_VAT: D(10)}, [OTHER_VAT]
    )
    assert folded_with_vat_debit.klass == "A3"


def test_a2_folded_on_the_gl_maps_the_line_expense_account_per_line():
    lines = [line(1, tax_code=code()), line(2, tax="5", amount="50", expense=EXP2, tax_code=code())]
    outcome = classify_invoice(ORG, lines, {EXP: D(110), EXP2: D(55)})
    assert outcome.klass == "A2"
    assert ref(outcome, 1) == ("A2", EXP) and ref(outcome, 2) == ("A2", EXP2)


@pytest.mark.parametrize(
    "ledger",
    [
        {ELSEWHERE: D(110)},  # tax folded into an expense account other than the line's
        {EXP: D(100), DEFER: D(9), ELSEWHERE: D(1)},  # the tax debit is not the tax
        {EXP: D(100), PAID: D(10)},  # debit on the paid account while the rule maps the deferral pair
        {EXP: D(100), DEFER: D(10), TAXACCT: D(5)},  # debits do not total the invoice
    ],
)
def test_a3_other_lands_the_folded_form_and_is_flagged(ledger):
    outcome = classify_invoice(ORG, [line(1, tax_code=code())], ledger)
    assert outcome.klass == "A3" and outcome.other and ref(outcome, 1) == ("A3", EXP)


def test_partial_split_is_a3():
    lines = [line(1, tax_code=code()), line(2, tax_code=code(tax_type="WITHHOLDING"))]
    outcome = classify_invoice(ORG, lines, {EXP: D(200), DEFER: D(10), ELSEWHERE: D(10)})
    assert outcome.klass == "A3"


def test_a_tax_account_debit_with_folded_expense_is_not_a2():
    outcome = classify_invoice(ORG, [line(1, tax_code=code())], {EXP: D(110), PAID: D(1)})
    assert outcome.klass == "A3"


def test_ledger_regime_is_decided_by_ledger_lines_not_by_the_snapshot_rule():
    # The snapshot rule says split, the ledger says folded: the ledger wins (the GL is history).
    outcome = classify_invoice(ORG, [line(1, tax_code=code())], {EXP: D(110)})
    assert ref(outcome, 1) == ("A2", EXP)


def test_malformed_facts_are_named_refusals():
    with pytest.raises(TaxAccountMappingError, match="line_id_missing_or_duplicate"):
        classify_invoice(ORG, [line(1), line(1)], None)


def test_untaxed_negative_invoice_does_not_raise_and_has_no_class():
    for debits in (None, {EXP: D(-40)}):
        outcome = classify_invoice(ORG, [line(1, tax="0", amount="-40")], debits)
        assert ref(outcome, 1) == (None, None) and outcome.klass is None
        assert outcome.legacy_posted is (debits is not None)


@pytest.mark.parametrize("facts", [dict(tax="10", amount="-100"), dict(tax="-10", amount="100")])
@pytest.mark.parametrize("debits", [None, {EXP: D(110)}])
def test_taxed_negative_invoice_is_class_x_with_null_refs(facts, debits):
    untaxed = line(2, tax="0", amount="5")
    outcome = classify_invoice(ORG, [line(1, tax_code=code(), **facts), untaxed], debits)
    assert outcome.klass == "X" and outcome.legacy_posted is (debits is not None) and not outcome.other
    assert ref(outcome, 1) == ("X", None) and ref(outcome, 2) == (None, None)


def test_class_counts_include_x():
    outcome = classify_invoice(ORG, [line(1, amount="-100", tax_code=code()), line(2, tax_code=code())], None)
    counts = class_counts([outcome])
    assert counts["X"] == dict(lines=2, invoices=1)
    assert counts["B1"] == counts["B2"] == dict(lines=0, invoices=0)
    assert class_counts([])["X"] == dict(lines=0, invoices=0)


def test_class_counts_report_lines_and_invoices():
    one = classify_invoice(ORG, [line(1, tax_code=code()), line(2, tax_code=None)], None)
    two = classify_invoice(ORG, [line(1, tax_code=code())], {EXP: D(110)})
    counts = class_counts([one, two])
    assert counts["B1"] == dict(lines=1, invoices=1) and counts["B2"] == dict(lines=1, invoices=1)
    assert counts["A2"] == dict(lines=1, invoices=1) and counts["A1"] == counts["A3"] == dict(lines=0, invoices=0)
    assert counts["a1_expense_elsewhere"] == dict(lines=0, invoices=0)


# ---- the enrolment map over a snapshot ----

INV = [str(UUID(int=n)) for n in (1001, 1002, 1003, 1004)]


def snapshot():
    src = sa.create_engine("sqlite://").connect()
    for stmt in (
        "CREATE TABLE supplier_invoice (invoice_id TEXT, organization_id TEXT, journal_entry_id TEXT)",
        "CREATE TABLE supplier_invoice_line (line_id TEXT, invoice_id TEXT, line_amount TEXT, tax_amount TEXT, "
        "expense_account_id TEXT, tax_code_id TEXT)",
        "CREATE TABLE tax_code (tax_code_id TEXT, organization_id TEXT, tax_type TEXT, tax_paid_account_id TEXT)",
        "CREATE TABLE account (account_id TEXT, deferral_pair_account_id TEXT)",
        "CREATE TABLE posted_ledger_line (journal_entry_id TEXT, account_id TEXT, debit_amount TEXT)",
    ):
        src.exec_driver_sql(stmt)
    insert = lambda table, rows: [  # noqa: E731
        src.execute(sa.text(f"INSERT INTO {table} VALUES ({','.join(f':{k}' for k in r)})"), r)  # noqa: S608
        for r in rows
    ]
    tc = str(UUID(int=77))
    insert("tax_code", [dict(a=tc, b=ORG, c="VAT", d=PAID)])
    insert("account", [dict(a=PAID, b=DEFER), dict(a=DEFER, b=None)])
    journals = {INV[0]: str(UUID(int=9001)), INV[1]: str(UUID(int=9002)), INV[2]: str(UUID(int=9003))}
    insert(
        "supplier_invoice",
        [
            dict(a=INV[0], b=ORG, c=journals[INV[0]]),
            dict(a=INV[1], b=ORG, c=journals[INV[1]]),
            dict(a=INV[2], b=ORG, c=journals[INV[2]]),
            dict(a=INV[3], b=ORG, c=None),
        ],
    )
    line_rows = [
        (INV[0], "100", "10", tc),  # A1
        (INV[1], "100", "10", tc),  # A2
        (INV[2], "100", "10", tc),  # A3
        (INV[3], "100", "10", tc),  # unposted -> B1
        (INV[3], "40", "0", None),  # untaxed
    ]
    insert(
        "supplier_invoice_line",
        [
            dict(a=str(UUID(int=2000 + i)), b=inv, c=amt, d=tax, e=EXP, f=code_id)
            for i, (inv, amt, tax, code_id) in enumerate(line_rows)
        ],
    )
    insert(
        "posted_ledger_line",
        [
            dict(a=journals[INV[0]], b=EXP, c="100"),
            dict(a=journals[INV[0]], b=DEFER, c="10"),
            dict(a=journals[INV[1]], b=EXP, c="110"),
            dict(a=journals[INV[2]], b=ELSEWHERE, c="110"),
        ],
    )
    return src


def test_enrolment_map_and_receipt_counts_over_a_snapshot():
    src = snapshot()
    maps = EnrolmentMaps(src, sa.create_engine("sqlite://").connect())
    built = maps.get("supplier_line_tax_accounts")
    expected = [DEFER, EXP, EXP, DEFER, None]
    got = [built[str(UUID(int=2000 + i))]["tax_account_ref"] for i in range(5)]
    assert got == expected
    counts = maps.tax_account_classes()
    assert {k: v["lines"] for k, v in counts.items()} == dict(A1=1, A2=1, A3=1, B1=1, B2=0, X=0, a1_expense_elsewhere=0)
    assert {k: v["invoices"] for k, v in counts.items()} == dict(
        A1=1, A2=1, A3=1, B1=1, B2=0, X=0, a1_expense_elsewhere=0
    )
    assert maps.summary()["supplier_line_tax_accounts"] == 5


def test_enrolment_map_uses_every_tax_codes_accounts_not_only_the_invoices_own():
    src = snapshot()
    # Another code (no line uses it) owns TAXACCT; the A1 invoice's journal also debits it, offset by less expense.
    src.execute(sa.text("INSERT INTO tax_code VALUES (:a, :b, 'VAT', :d)"), dict(a=str(UUID(int=78)), b=ORG, d=TAXACCT))
    src.execute(sa.text("INSERT INTO account VALUES (:a, NULL)"), dict(a=TAXACCT))
    journal = str(UUID(int=9001))
    src.execute(
        sa.text("UPDATE posted_ledger_line SET debit_amount = '90' WHERE journal_entry_id = :j AND account_id = :a"),
        dict(j=journal, a=EXP),
    )
    src.execute(sa.text("INSERT INTO posted_ledger_line VALUES (:j, :a, '10')"), dict(j=journal, a=TAXACCT))
    maps = EnrolmentMaps(src, src)
    built = maps.get("supplier_line_tax_accounts")
    assert built[str(UUID(int=2000))]["tax_account_ref"] == EXP  # A3 now: the folded form
    assert maps.tax_account_classes()["A1"] == dict(lines=0, invoices=0)
    assert maps.tax_account_classes()["A3"]["invoices"] == 2


def test_one_x_invoice_and_one_untaxed_negative_invoice_do_not_stop_the_others():
    src = snapshot()
    tc = str(UUID(int=77))
    x_inv, credit_note = str(UUID(int=1005)), str(UUID(int=1006))
    for inv in (x_inv, credit_note):
        src.execute(sa.text("INSERT INTO supplier_invoice VALUES (:a, :b, NULL)"), dict(a=inv, b=ORG))
    for n, (inv, amt, tax, code_id) in enumerate(((x_inv, "-100", "10", tc), (credit_note, "-40", "0", None))):
        src.execute(
            sa.text("INSERT INTO supplier_invoice_line VALUES (:a, :b, :c, :d, :e, :f)"),
            dict(a=str(UUID(int=3000 + n)), b=inv, c=amt, d=tax, e=EXP, f=code_id),
        )
    maps = EnrolmentMaps(src, src)
    built = maps.get("supplier_line_tax_accounts")
    assert [built[str(UUID(int=2000 + i))]["tax_account_ref"] for i in range(5)] == [DEFER, EXP, EXP, DEFER, None]
    assert built[str(UUID(int=3000))]["tax_account_ref"] is None
    assert built[str(UUID(int=3001))]["tax_account_ref"] is None
    counts = maps.tax_account_classes()
    assert counts["X"] == dict(lines=1, invoices=1)
    assert {k: v["invoices"] for k, v in counts.items() if k != "X"} == dict(
        A1=1, A2=1, A3=1, B1=1, B2=0, a1_expense_elsewhere=0
    )


def test_missing_tax_code_or_account_row_refuses_the_map():
    src = snapshot()
    src.execute(sa.text("DELETE FROM account WHERE account_id = :a"), dict(a=DEFER))
    with pytest.raises(TaxAccountMappingError, match="tax_deferral_account_row_missing"):
        EnrolmentMaps(src, src).get("supplier_line_tax_accounts")
    src = snapshot()
    src.exec_driver_sql("DELETE FROM tax_code")
    with pytest.raises(TaxAccountMappingError, match="tax_code_row_missing"):
        EnrolmentMaps(src, src).get("supplier_line_tax_accounts")


def test_supplier_invoice_lines_pairing_declares_the_lookup():
    pairing = next(p for p in ALL_WAVE_PAIRINGS if p.destination == "mod_payables.supplier_invoice_lines")
    assert pairing.lookups["tax_account_ref"] == ("supplier_line_tax_accounts", "id", "tax_account_ref")
    assert "supplier_line_tax_accounts" in MAP_BUILDERS
