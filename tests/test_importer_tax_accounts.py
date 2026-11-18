"""tax_account_ref derivation: the two-regime rule, the enrolment map and its receipt counts."""

from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa

from erp_v2.importer.enrolment import MAP_BUILDERS, EnrolmentMaps
from erp_v2.importer.tax_accounts import (
    DeferralFacts,
    LineFacts,
    TaxAccountMappingError,
    TaxCodeFacts,
    class_counts,
    classify_invoice,
    classify_snapshot,
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

OLD_KEYS = ("A1", "A2", "A3", "B1", "B2", "X", "a1_expense_elsewhere")


def base(counts, field):
    """The pre-deferral count keys only (the new A1D / a3_reasons keys are asserted by their own tests)."""
    return {k: counts[k][field] for k in OLD_KEYS}


INV = [str(UUID(int=n)) for n in (1001, 1002, 1003, 1004)]


def snapshot():
    src = sa.create_engine("sqlite://").connect()
    for stmt in (
        "CREATE TABLE supplier_invoice (invoice_id TEXT, organization_id TEXT, journal_entry_id TEXT, "
        "ap_control_account_id TEXT)",
        "CREATE TABLE supplier_invoice_line (line_id TEXT, invoice_id TEXT, line_amount TEXT, tax_amount TEXT, "
        "expense_account_id TEXT, tax_code_id TEXT)",
        "CREATE TABLE tax_code (tax_code_id TEXT, organization_id TEXT, tax_type TEXT, tax_paid_account_id TEXT)",
        "CREATE TABLE account (account_id TEXT, deferral_pair_account_id TEXT)",
        "CREATE TABLE posted_ledger_line (journal_entry_id TEXT, account_id TEXT, debit_amount TEXT, "
        "credit_amount TEXT)",
        "CREATE TABLE journal_entry (journal_entry_id TEXT, source_module TEXT, source_document_type TEXT, "
        "source_document_id TEXT, status TEXT)",
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
            dict(a=INV[0], b=ORG, c=journals[INV[0]], d=None),
            dict(a=INV[1], b=ORG, c=journals[INV[1]], d=None),
            dict(a=INV[2], b=ORG, c=journals[INV[2]], d=None),
            dict(a=INV[3], b=ORG, c=None, d=None),
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
            dict(a=journals[INV[0]], b=EXP, c="100", d=None),
            dict(a=journals[INV[0]], b=DEFER, c="10", d=None),
            dict(a=journals[INV[1]], b=EXP, c="110", d=None),
            dict(a=journals[INV[2]], b=ELSEWHERE, c="110", d=None),
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
    assert base(counts, "lines") == dict(A1=1, A2=1, A3=1, B1=1, B2=0, X=0, a1_expense_elsewhere=0)
    assert base(counts, "invoices") == dict(A1=1, A2=1, A3=1, B1=1, B2=0, X=0, a1_expense_elsewhere=0)
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
    src.execute(sa.text("INSERT INTO posted_ledger_line VALUES (:j, :a, '10', NULL)"), dict(j=journal, a=TAXACCT))
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
        src.execute(sa.text("INSERT INTO supplier_invoice VALUES (:a, :b, NULL, NULL)"), dict(a=inv, b=ORG))
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
    assert {k: v for k, v in base(counts, "invoices").items() if k != "X"} == dict(
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


# ---- NULL expense account: keyed by a sentinel (operator-oracle semantics), never a raise ----


def test_legacy_posted_a1_with_a_null_expense_untaxed_line_is_a1_expense_elsewhere():
    lines = [line(1, tax_code=code()), line(2, tax="0", amount="40", expense=None)]
    outcome = classify_invoice(ORG, lines, {EXP: D(100), DEFER: D(10), ELSEWHERE: D(40)})
    assert outcome.klass == "A1" and outcome.expense_elsewhere
    assert ref(outcome, 1) == ("A1", DEFER) and ref(outcome, 2) == (None, None)


def test_folded_invoice_with_a_null_expense_line_is_a3_never_a2():
    lines = [line(1, expense=None), line(2, amount="50", tax="0", expense=EXP)]
    outcome = classify_invoice(ORG, lines, {EXP: D(160)})
    assert outcome.klass == "A3" and outcome.other
    assert ref(outcome, 1) == ("A3", None)


def test_unposted_taxed_null_expense_line_is_b2_with_a_null_ref():
    outcome = classify_invoice(ORG, [line(1, expense=None)], None)
    assert outcome.klass == "B" and ref(outcome, 1) == ("B2", None)


def _row(invoice, journal, n, amount, tax, expense, tax_code_id=None):
    return dict(
        invoice_id=invoice,
        organization_id=ORG,
        journal_entry_id=journal,
        line_id=str(UUID(int=500 + n)),
        line_amount=amount,
        tax_amount=tax,
        expense_account_id=expense,
        tax_code_id=tax_code_id,
        code_row=None,
        code_organization_id=None,
        tax_type=None,
        tax_paid_account_id=None,
        paid_row=None,
        deferral_pair_account_id=None,
        deferral_row=None,
    )


def test_snapshot_with_null_expense_lines_classifies_every_invoice():
    j1, j2 = str(UUID(int=9101)), str(UUID(int=9102))
    i1, i2, i3, i4 = (str(UUID(int=n)) for n in (1, 2, 3, 4))
    rows = [
        _row(i1, j1, 1, "100", "10", None),  # posted, folded, null expense -> A3
        _row(i2, None, 2, "100", "10", None),  # unposted -> B2, NULL ref
        _row(i3, j2, 3, "100", "10", EXP),  # normal posted folded -> A2
        _row(i4, None, 4, "100", "10", EXP),  # normal unposted -> B2
    ]
    ledger = [
        dict(journal_entry_id=j1, account_id=EXP, debit_amount="110"),
        dict(journal_entry_id=j2, account_id=EXP, debit_amount="110"),
    ]
    out = classify_snapshot(rows, ledger)
    assert {k: v.klass for k, v in out.items()} == {i1: "A3", i2: "B", i3: "A2", i4: "B"}
    assert out[i2].lines[str(UUID(int=502))].ref is None
    assert out[i4].lines[str(UUID(int=504))].ref == EXP


def test_null_account_ledger_row_is_a_named_raise():
    with pytest.raises(TaxAccountMappingError, match=r"^ledger_account_missing$"):
        classify_invoice(ORG, [line(1)], {None: D(110)})
    j, i = str(UUID(int=9101)), str(UUID(int=1))
    with pytest.raises(TaxAccountMappingError, match=r"^ledger_account_missing$"):
        classify_snapshot(
            [_row(i, j, 1, "100", "10", EXP)], [dict(journal_entry_id=j, account_id=None, debit_amount="110")]
        )


# ---- A1D: the legacy VAT-deferral journal (conversion spec section 9) ----

CTRL, TAX1440 = str(UUID(int=40)), str(UUID(int=41))


def deferral_invoice(inv_net, journals, *, tax="10", amount="100", not_posted=0, debits=None, lines=None):
    """Classify one taxed invoice (EXP 100 + tax on the DEFER split account) with deferral journals."""
    lines = lines or [line(1, tax=tax, amount=amount, tax_code=code())]
    if debits is None:
        debits = {a: v for a, v in inv_net.items() if v > 0 and a != CTRL}
    return classify_invoice(
        ORG,
        lines,
        debits,
        [PAID, DEFER],
        invoice_net=inv_net,
        deferral=DeferralFacts(tuple(journals), not_posted),
        control_account=CTRL,
    )


FOLDED = {EXP: D(110), CTRL: D(-110)}  # legacy posted the invoice folded
EQUAL = {DEFER: D(10), EXP: D(-10)}  # Dr 1455 / Cr expense


def test_a1d_deferral_journal_equal_to_the_vat_is_a_correct_split_on_the_deferral_account():
    outcome = deferral_invoice(FOLDED, [EQUAL])
    assert outcome.klass == "A1D" and not outcome.other and not outcome.expense_elsewhere
    assert ref(outcome, 1) == ("A1D", DEFER)
    assert outcome.deferral_journals == 1 and outcome.reason is None


def test_a1d_credit_side_may_be_the_tax_paid_account():
    # Invoice posted split on the paid account (Dr 1440 10), the deferral journal moves it: Dr 1455 / Cr 1440.
    outcome = deferral_invoice(
        {EXP: D(100), PAID: D(10), CTRL: D(-110)}, [{DEFER: D(10), PAID: D(-10)}], debits={EXP: D(100), PAID: D(10)}
    )
    assert outcome.klass == "A1D" and ref(outcome, 1) == ("A1D", DEFER)


def test_a1d_expense_elsewhere_is_counted_apart_from_a1():
    outcome = deferral_invoice({ELSEWHERE: D(110), CTRL: D(-110)}, [{DEFER: D(10), ELSEWHERE: D(-10)}])
    assert outcome.klass == "A1D" and outcome.expense_elsewhere
    counts = class_counts([outcome])
    assert counts["a1d_expense_elsewhere"] == dict(lines=1, invoices=1)
    assert counts["a1_expense_elsewhere"] == dict(lines=0, invoices=0)
    assert counts["A1D"] == dict(lines=1, invoices=1) and counts["A3"] == dict(lines=0, invoices=0)


def test_unequal_deferral_journal_is_a3_deferral_amount_mismatch():
    outcome = deferral_invoice(FOLDED, [{DEFER: D(9), EXP: D(-9)}])
    assert outcome.klass == "A3" and outcome.other and outcome.reason == "deferral_amount_mismatch"
    assert ref(outcome, 1) == ("A3", EXP)  # lands the folded form, refused by the verifiers
    assert class_counts([outcome])["a3_reasons"]["deferral_amount_mismatch"] == dict(lines=1, invoices=1)


def test_equal_debit_but_unbalanced_combined_is_a3_deferral_combined_unbalanced():
    # Split account net equals the tax, but a stray net on the expense side breaks the invoice total.
    outcome = deferral_invoice(FOLDED, [{DEFER: D(10), EXP: D(-12)}])
    assert outcome.klass == "A3" and outcome.reason == "deferral_combined_unbalanced"
    other_tax_account = deferral_invoice(FOLDED, [{DEFER: D(10), PAID: D(5), EXP: D(-15)}])
    assert other_tax_account.reason == "deferral_combined_unbalanced"  # another code's account has a net
    negative = deferral_invoice(
        {EXP: D(110), ELSEWHERE: D(0), CTRL: D(-110)}, [{DEFER: D(10), EXP: D(-10), ELSEWHERE: D(-5)}]
    )
    assert negative.reason == "deferral_combined_unbalanced"


def test_two_deferral_journals_are_never_summed():
    outcome = deferral_invoice(FOLDED, [{DEFER: D(5), EXP: D(-5)}, {DEFER: D(5), EXP: D(-5)}])
    assert outcome.klass == "A3" and outcome.reason == "deferral_journal_multiple"
    assert outcome.deferral_journals == 2
    assert class_counts([outcome])["a3_reasons"]["deferral_journal_multiple"] == dict(lines=1, invoices=1)


def test_deferral_journal_on_an_invoice_whose_journal_already_split_is_a3():
    split_net = {EXP: D(100), DEFER: D(10), CTRL: D(-110)}
    outcome = deferral_invoice(split_net, [EQUAL])
    assert outcome.klass == "A3" and outcome.reason == "deferral_on_split_invoice"
    # The same holds when the A1 test would not pass (an unrelated extra debit) but the split account is debited.
    outcome = deferral_invoice({EXP: D(110), DEFER: D(10), CTRL: D(-120)}, [EQUAL])
    assert outcome.reason == "deferral_on_split_invoice"


def test_not_posted_deferral_journal_is_counted_and_ignored():
    outcome = deferral_invoice(FOLDED, [], not_posted=1)
    # Ignored: the invoice is folded on the GL, so it is A2 (no deferral journal to rely on), counted for the receipt.
    assert outcome.klass == "A2" and outcome.deferral_not_posted == 1 and outcome.deferral_journals == 0
    assert class_counts([outcome])["deferral_journal_not_posted"] == dict(lines=0, invoices=1)


def test_no_deferral_journal_falls_back_to_a2_then_a3_other():
    assert classify_invoice(ORG, [line(1, tax_code=code())], {EXP: D(110)}, [PAID, DEFER]).klass == "A2"
    outcome = classify_invoice(ORG, [line(1, tax_code=code())], {ELSEWHERE: D(110)}, [PAID, DEFER])
    assert outcome.klass == "A3" and outcome.reason == "other"
    assert class_counts([outcome])["a3_reasons"]["other"] == dict(lines=1, invoices=1)


def test_an_invoice_with_a_deferral_journal_is_never_a2():
    # The deferral journal does not fit A1D (a split net differs); the folded invoice journal must not rescue it.
    outcome = deferral_invoice(FOLDED, [{DEFER: D(1), EXP: D(-1)}])
    assert outcome.klass == "A3" and outcome.reason == "deferral_amount_mismatch"


def test_untaxed_invoice_with_a_deferral_journal_is_counted_informationally():
    outcome = classify_invoice(
        ORG,
        [line(1, tax="0")],
        {EXP: D(100)},
        [PAID, DEFER],
        invoice_net={EXP: D(100), CTRL: D(-100)},
        deferral=DeferralFacts(({DEFER: D(10), EXP: D(-10)},)),
        control_account=CTRL,
    )
    assert outcome.klass is None and outcome.deferral_journals == 1
    assert class_counts([outcome])["untaxed_with_deferral"] == dict(lines=0, invoices=1)


def test_unposted_invoice_ignores_deferral_and_a_null_ledger_account_is_named():
    outcome = classify_invoice(
        ORG, [line(1, tax_code=code())], None, deferral=DeferralFacts(({DEFER: D(10), EXP: D(-10)},))
    )
    assert outcome.klass == "B"
    with pytest.raises(TaxAccountMappingError, match=r"^ledger_account_missing$"):
        deferral_invoice(FOLDED, [{None: D(10)}])


def test_a1_without_a_deferral_journal_is_unchanged_and_a1_expense_elsewhere_stays_a1_only():
    outcome = classify_invoice(ORG, [line(1, tax_code=code())], {ELSEWHERE: D(100), DEFER: D(10)}, [PAID, DEFER])
    assert outcome.klass == "A1" and outcome.expense_elsewhere
    counts = class_counts([outcome])
    assert counts["a1_expense_elsewhere"] == dict(lines=1, invoices=1)
    assert counts["a1d_expense_elsewhere"] == dict(lines=0, invoices=0)


# ---- classify_snapshot / enrolment map with deferral journals ----

DEF_J = str(UUID(int=8001))


def _deferral_snapshot(*, status="POSTED", extra_journal=False, credit="10", debit="10"):
    src = snapshot()
    inv = str(UUID(int=1100))
    ctrl = str(UUID(int=50))
    journal = str(UUID(int=9100))
    src.execute(sa.text("INSERT INTO supplier_invoice VALUES (:a, :b, :c, :d)"), dict(a=inv, b=ORG, c=journal, d=ctrl))
    src.execute(
        sa.text("INSERT INTO supplier_invoice_line VALUES (:a, :b, 100, 10, :e, :t)"),
        dict(a=str(UUID(int=4000)), b=inv, e=EXP, t=str(UUID(int=77))),
    )
    ledger = [
        (journal, EXP, "110", None),
        (journal, ctrl, None, "110"),
        (DEF_J, DEFER, debit, None),
        (DEF_J, EXP, None, credit),
    ]
    for row in ledger:
        src.execute(
            sa.text("INSERT INTO posted_ledger_line VALUES (:a, :b, :c, :d)"), dict(zip("abcd", row, strict=True))
        )
    headers = [(DEF_J, "AP", "SUPPLIER_INVOICE_VAT_DEFERRAL", inv, status)]
    if extra_journal:
        headers.append((str(UUID(int=8002)), "AP", "SUPPLIER_INVOICE_VAT_DEFERRAL", inv, "POSTED"))
    for row in headers:
        src.execute(
            sa.text("INSERT INTO journal_entry VALUES (:a, :b, :c, :d, :e)"), dict(zip("abcde", row, strict=True))
        )
    return src


def _classes(src):
    maps = EnrolmentMaps(src, src)
    built = maps.get("supplier_line_tax_accounts")
    return built, maps.tax_account_classes()


def test_snapshot_deferral_equal_is_a1d_and_maps_the_deferral_account():
    built, counts = _classes(_deferral_snapshot())
    assert built[str(UUID(int=4000))]["tax_account_ref"] == DEFER
    assert (
        counts["A1D"] == dict(lines=1, invoices=1) and counts["A3"]["invoices"] == 1
    )  # snapshot()'s own A3 and A2 untouched
    assert counts["a3_reasons"]["other"]["invoices"] == 1 and counts["A2"]["invoices"] == 1


def test_snapshot_deferral_unequal_missing_multiple_and_not_posted():
    built, counts = _classes(_deferral_snapshot(debit="9", credit="9"))
    assert counts["A1D"]["invoices"] == 0 and counts["a3_reasons"]["deferral_amount_mismatch"] == dict(
        lines=1, invoices=1
    )
    assert built[str(UUID(int=4000))]["tax_account_ref"] == EXP
    built, counts = _classes(_deferral_snapshot(extra_journal=True))
    assert counts["a3_reasons"]["deferral_journal_multiple"]["invoices"] == 1
    built, counts = _classes(_deferral_snapshot(status="DRAFT"))
    # the not-posted journal is ignored and counted: the folded invoice journal is A2 again
    assert counts["deferral_journal_not_posted"]["invoices"] == 1 and counts["A1D"]["invoices"] == 0
    assert built[str(UUID(int=4000))]["tax_account_ref"] == EXP
    src = _deferral_snapshot()
    src.exec_driver_sql("DELETE FROM journal_entry")  # missing deferral journal: folded invoice is A2
    built, counts = _classes(src)
    assert counts["A1D"]["invoices"] == 0 and counts["A2"]["invoices"] == 2
