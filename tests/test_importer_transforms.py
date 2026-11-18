"""B-3 importer transforms: one test per rule, the bridge partition, the parity delta, and the executor wiring.

Documentation-range data only (no estate identifiers): accounts and invoices are synthetic UUIDs.
"""

import json
import os
import stat
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa

from erp_v2.importer.enrolment import EnrolmentMaps
from erp_v2.importer.executor import execute_transform
from erp_v2.importer.spec import DestinationColumn, build_spec
from erp_v2.importer.tax_accounts import LineFacts, TaxCodeFacts, classify_invoice
from erp_v2.importer.transforms import (
    CARRY,
    QUEUE,
    RULES,
    T_EXP_FALLBACK,
    T_EXP_LEGACY,
    T_ITEM_NONSTOCK,
    T_ROUND_2DP,
    T_UNPOSTED_CORRECT,
    TRANSFORM,
    AccountFacts,
    InvoiceFacts,
    TransformLedger,
    TransformParams,
    TransformParamsError,
    build_transforms,
    transform_invoice,
)
from erp_v2.importer.waves36 import WAVE3_PAIRINGS

D = Decimal


def uid(n: int) -> str:
    return str(UUID(int=n))


ORG = uid(1)
EXP, EXP2, ELSEWHERE, ELSEWHERE2, FALLBACK = (uid(n) for n in (10, 11, 30, 31, 40))
CTRL, STAFF, PAYROLL, TAXACCT, RELEASE, PAID = (uid(n) for n in (50, 51, 52, 21, 22, 20))
INACTIVE = uid(60)
ACCOUNTS = {
    EXP: AccountFacts("5000"),
    EXP2: AccountFacts("5010"),
    ELSEWHERE: AccountFacts("5100"),
    ELSEWHERE2: AccountFacts("5110"),
    FALLBACK: AccountFacts("5999"),
    CTRL: AccountFacts("2000"),
    STAFF: AccountFacts("1410"),
    PAYROLL: AccountFacts("2040"),
    TAXACCT: AccountFacts("1455"),
    RELEASE: AccountFacts("1440"),
    PAID: AccountFacts("1440"),
    INACTIVE: AccountFacts("5200", active=False),
}


def vat(deferral=TAXACCT, paid=PAID, tax_type="VAT", org=ORG):
    return TaxCodeFacts(org, tax_type, paid, deferral)


def line(n, amount="100", tax="0", expense=EXP, code=None):
    return LineFacts(uid(1000 + n), D(amount), D(tax), expense, code)


def lid(n):
    return uid(1000 + n)


def invoice(
    lines,
    *,
    n=1,
    status="DRAFT",
    ledger=None,
    items=None,
    sub=None,
    tax=None,
    total=None,
    deferral=None,
):
    """InvoiceFacts with the REAL classifier's outcome. Header defaults to the lines' sums."""

    sub_ = D(sub) if sub is not None else sum((x.line_amount for x in lines), D(0))
    tax_ = D(tax) if tax is not None else sum((x.tax_amount for x in lines), D(0))
    total_ = D(total) if total is not None else sub_ + tax_
    outcome = classify_invoice(ORG, lines, ledger, deferral=deferral)
    return InvoiceFacts(uid(9000 + n), ORG, status, sub_, tax_, total_, tuple(lines), outcome, items or {})


def run(inv):
    return transform_invoice(inv, FALLBACK, ACCOUNTS)


def rules(outcome):
    return sorted({b.rule for b in outcome.bridge})


# ---- the required declared input ----


def test_fallback_account_is_a_required_declared_input():
    with pytest.raises(TransformParamsError) as exc:
        TransformParams.from_mapping({}).validate(ACCOUNTS)
    assert exc.value.code == "fallback_expense_account_missing"
    with pytest.raises(TransformParamsError) as exc:
        TransformParams.from_mapping({"import.fallback_expense_account": "   "}).validate(ACCOUNTS)
    assert exc.value.code == "fallback_expense_account_missing"


@pytest.mark.parametrize(
    "account,code",
    [
        (CTRL, "fallback_expense_account_invalid:expense_account_is_control"),
        (STAFF, "fallback_expense_account_invalid:expense_account_is_control"),
        (PAYROLL, "fallback_expense_account_invalid:expense_account_is_control"),
        (TAXACCT, "fallback_expense_account_invalid:expense_account_is_tax_role"),
        (RELEASE, "fallback_expense_account_invalid:expense_account_is_tax_role"),
        (INACTIVE, "fallback_expense_account_invalid:account_inactive_or_not_postable"),
        (uid(999), "fallback_expense_account_invalid:account_unknown"),
    ],
)
def test_fallback_account_must_be_a_usable_expense_account(account, code):
    with pytest.raises(TransformParamsError) as exc:
        TransformParams.from_mapping({"import.fallback_expense_account": account}).validate(ACCOUNTS)
    assert exc.value.code == code


def test_fallback_account_must_be_a_uuid_and_validates_to_canonical():
    with pytest.raises(TransformParamsError) as exc:
        TransformParams.from_mapping({"import.fallback_expense_account": "5999"})
    assert exc.value.code == "fallback_expense_account_not_a_uuid"
    assert TransformParams.from_mapping({"import.fallback_expense_account": FALLBACK.upper()}).validate(ACCOUNTS) == (
        FALLBACK
    )


# ---- T-EXP-FALLBACK ----


def test_null_expense_line_lands_on_the_fallback_with_a_bridge_line():
    out = run(invoice([line(1, expense=None), line(2, amount="50", expense=EXP)]))
    assert out.bucket == TRANSFORM and out.lands
    assert out.lines[lid(1)].posting_account_ref == FALLBACK and out.lines[lid(2)].posting_account_ref == EXP
    (bridge,) = out.bridge
    assert (bridge.rule, bridge.line_id, bridge.legacy_account, bridge.v2_account) == (
        T_EXP_FALLBACK,
        lid(1),
        None,
        FALLBACK,
    )
    assert (bridge.legacy_amount, bridge.v2_amount, bridge.delta) == (D(100), D(100), D(0))


# ---- T-EXP-LEGACY ----


def _a1_elsewhere(lines, ledger, **kw):
    inv = invoice(lines, ledger=ledger, status="POSTED", **kw)
    assert inv.tax.klass == "A1" and inv.tax.expense_elsewhere
    return inv


def test_expense_posted_elsewhere_follows_the_single_legacy_gl_account():
    inv = _a1_elsewhere([line(1, tax="10", code=vat())], {ELSEWHERE: D(100), TAXACCT: D(10)})
    out = run(inv)
    assert out.lines[lid(1)].posting_account_ref == ELSEWHERE
    assert out.lines[lid(1)].tax_account_ref == TAXACCT  # legacy GL history: the classifier's A1 ref, unchanged
    (bridge,) = out.bridge
    assert (bridge.rule, bridge.legacy_account, bridge.v2_account) == (T_EXP_LEGACY, EXP, ELSEWHERE)
    assert out.bucket == TRANSFORM


def test_null_expense_on_a_posted_a1_follows_the_legacy_gl_account_not_the_fallback():
    inv = _a1_elsewhere([line(1, expense=None, tax="10", code=vat())], {ELSEWHERE: D(100), TAXACCT: D(10)})
    out = run(inv)
    assert out.lines[lid(1)].posting_account_ref == ELSEWHERE
    assert rules(out) == [T_EXP_LEGACY]


def test_two_groups_pair_with_two_legacy_accounts_by_distinct_sums():
    inv = _a1_elsewhere(
        [line(1, amount="100", expense=EXP, tax="10", code=vat()), line(2, amount="40", expense=EXP2)],
        {ELSEWHERE: D(40), ELSEWHERE2: D(100), TAXACCT: D(10)},
    )
    out = run(inv)
    assert out.lines[lid(1)].posting_account_ref == ELSEWHERE2 and out.lines[lid(2)].posting_account_ref == ELSEWHERE


def test_ambiguous_legacy_mapping_keeps_own_accounts_and_queues_but_still_lands():
    # equal group sums (50, 50) against equal legacy sums: no unique pairing; the NULL line is a third group
    inv = _a1_elsewhere(
        [
            line(1, amount="50", expense=EXP, tax="5", code=vat()),
            line(2, amount="50", expense=EXP2),
            line(3, amount="10", expense=None),
        ],
        {ELSEWHERE: D(50), ELSEWHERE2: D(60), TAXACCT: D(5)},
    )
    out = run(inv)
    assert out.bucket == QUEUE and out.lands and out.queue_reason == "expense_elsewhere_ambiguous"
    assert out.lines[lid(1)].posting_account_ref == EXP and out.lines[lid(2)].posting_account_ref == EXP2
    assert out.lines[lid(3)].posting_account_ref == FALLBACK  # the NULL line still cannot land without an account
    assert T_EXP_LEGACY not in rules(out)


def test_a_legacy_account_that_is_a_control_account_is_not_followed():
    inv = _a1_elsewhere([line(1, tax="10", code=vat())], {CTRL: D(100), TAXACCT: D(10)})
    out = run(inv)
    assert out.bucket == QUEUE and out.queue_reason == "expense_elsewhere_ambiguous"
    assert out.lines[lid(1)].posting_account_ref == EXP


def test_expense_on_its_own_account_is_carried():
    inv = invoice([line(1, tax="10", code=vat())], ledger={EXP: D(100), TAXACCT: D(10)}, status="POSTED")
    out = run(inv)
    assert out.bucket == CARRY and out.bridge == ()


# ---- T-ROUND-2DP ----


def test_round_half_even_to_two_decimals_with_residue_on_the_largest_line():
    # 0.125 -> 0.12, 0.135 -> 0.14 (half-even); 10.005 -> 10.00; header 20.265 -> 20.26 vs lines 0.12+0.14+10.00 etc.
    lines = [line(1, amount="10.005", expense=EXP), line(2, amount="10.015", expense=EXP2)]
    inv = invoice(lines, sub="20.02", total="20.02")
    out = run(inv)
    assert out.bucket == TRANSFORM and out.lands
    got = {k: v.line_amount for k, v in out.lines.items()}
    assert got == {lid(1): D("10.00"), lid(2): D("10.02")} and sum(got.values()) == D("20.02")
    assert out.header["subtotal"] == D("20.02") and out.header["total_amount"] == D("20.02")
    for b in out.bridge:
        if b.scope == "line":
            assert b.rule == T_ROUND_2DP and abs(b.delta) < D("0.01")


def test_residue_goes_on_the_largest_line_and_every_line_moves_less_than_a_cent():
    lines = [line(1, amount="33.334", expense=EXP), line(2, amount="33.334", expense=EXP2), line(3, amount="33.334")]
    out = run(invoice(lines, sub="100.00", total="100.00"))
    amounts = sorted(v.line_amount for v in out.lines.values())
    assert amounts == [D("33.33"), D("33.33"), D("33.34")] and sum(amounts) == D("100.00")
    assert all(abs(b.delta) < D("0.01") for b in out.bridge if b.scope == "line")


def test_rounding_that_cannot_stay_under_a_cent_per_line_is_queued():
    out = run(invoice([line(1, amount="10.004")], sub="10.50", total="10.50"))
    assert out.bucket == QUEUE and not out.lands and out.queue_reason == "rounding_residue_too_large"


def test_two_decimal_invoice_is_never_touched_by_the_rounding_rule():
    out = run(invoice([line(1, amount="10.12", tax="0.00")]))
    assert out.bucket == CARRY and out.header["total_amount"] == D("10.12")


def test_tax_rounds_and_the_total_stays_the_sum():
    inv = invoice([line(1, amount="100.00", tax="7.5049", code=vat())], sub="100.00", tax="7.5049", total="107.5049")
    out = run(inv)
    assert out.lines[lid(1)].tax_amount == D("7.50")
    assert (out.header["subtotal"], out.header["tax_amount"], out.header["total_amount"]) == (
        D("100.00"),
        D("7.50"),
        D("107.50"),
    )


# ---- T-ITEM-NONSTOCK ----


def test_item_linked_line_lands_as_a_plain_expense_line_with_the_item_as_provenance():
    out = run(invoice([line(1, amount="25.00")], items={lid(1): "ITEM-7"}))
    assert out.bucket == TRANSFORM and out.lands
    assert out.lines[lid(1)].posting_account_ref == EXP and out.lines[lid(1)].line_amount == D("25.00")
    (bridge,) = out.bridge
    assert bridge.rule == T_ITEM_NONSTOCK and "ITEM-7" in bridge.note and bridge.delta == 0


# ---- T-UNPOSTED-CORRECT (D-M2) ----


def _line_ok_for_v1(posting, tax_ref, tax):
    """What V1/V2 (ledger_roles.invoice_account_violation) would require of a landed unposted line."""
    if posting in (CTRL, STAFF, PAYROLL, TAXACCT, RELEASE):
        return False
    if tax > 0:
        return tax_ref is not None and tax_ref != posting and ACCOUNTS[tax_ref].code == "1455"
    return True


@pytest.mark.parametrize("status", ["DRAFT", "SUBMITTED", "APPROVED"])
def test_unposted_vat_line_lands_on_1455_not_folded(status):
    out = run(invoice([line(1, amount="100", tax="7.5", code=vat())], status=status))
    only = out.lines[lid(1)]
    assert out.bucket == CARRY  # the snapshot split already is the v2 coding: nothing to transform
    assert only.tax_account_ref == TAXACCT and only.posting_account_ref == EXP
    assert _line_ok_for_v1(only.posting_account_ref, only.tax_account_ref, only.tax_amount)


def test_unposted_wht_line_has_the_wht_removed_so_the_total_is_the_gross():
    wht = TaxCodeFacts(ORG, "WITHHOLDING", PAID, None)
    inv = invoice(
        [line(1, amount="100", tax="7.5", code=vat()), line(2, amount="200", tax="10", code=wht)],
        status="SUBMITTED",
        sub="300",
        tax="17.5",
        total="317.5",
    )
    out = run(inv)
    assert out.bucket == TRANSFORM and out.lands
    assert out.lines[lid(2)].tax_amount == D(0) and out.lines[lid(2)].tax_account_ref is None
    assert out.lines[lid(1)].tax_account_ref == TAXACCT
    assert (out.header["subtotal"], out.header["tax_amount"], out.header["total_amount"]) == (
        D(300),
        D("7.5"),
        D("307.5"),
    )
    assert out.header_delta == D("-10")
    header_line = next(b for b in out.bridge if b.scope == "header")
    assert (header_line.rule, header_line.legacy_amount, header_line.v2_amount) == (
        T_UNPOSTED_CORRECT,
        D("317.5"),
        D("307.5"),
    )
    assert "total is the gross" in header_line.note
    line_line = next(b for b in out.bridge if b.scope == "line")
    assert "wht_removed 10" in line_line.note and "payment-time" in line_line.note


def test_unposted_taxed_line_without_a_valid_code_is_queued_not_folded():
    for code in (None, TaxCodeFacts(ORG, "STAMP_DUTY", PAID, None), vat(org=uid(2)), vat(paid=None, deferral=None)):
        out = run(invoice([line(1, tax="5", code=code)], status="DRAFT"))
        assert out.bucket == QUEUE and not out.lands, code
        assert out.queue_reason == "taxed_line_without_valid_tax_code"


def test_vat_split_account_must_be_the_declared_input_tax_account():
    out = run(invoice([line(1, tax="5", code=vat(deferral=None, paid=PAID))], status="DRAFT"))  # PAID is 1440
    assert out.bucket == QUEUE and out.queue_reason == "tax_account_not_input_tax"


@pytest.mark.parametrize(
    "bad,reason",
    [
        (CTRL, "expense_account_is_control"),
        (STAFF, "expense_account_is_control"),
        (PAYROLL, "expense_account_is_control"),
        (TAXACCT, "expense_account_is_tax_role"),
        (RELEASE, "expense_account_is_tax_role"),
    ],
)
def test_unposted_expense_on_a_control_or_tax_role_account_is_queued(bad, reason):
    out = run(invoice([line(1, expense=bad)], status="DRAFT"))
    assert out.bucket == QUEUE and not out.lands and out.queue_reason == reason


def test_unposted_invoice_with_a_posted_deferral_journal_is_queued():
    from erp_v2.importer.tax_accounts import DeferralFacts

    deferral = DeferralFacts(({TAXACCT: D(5), EXP: D(-5)},), 0, (uid(8000),))
    inv = invoice([line(1, tax="5", code=vat())], status="SUBMITTED", deferral=deferral)
    out = run(inv)
    assert out.bucket == QUEUE and out.queue_reason == "deferral_on_unposted_invoice"


def test_no_landed_unposted_line_is_in_the_folded_or_control_form():
    cases = [
        invoice([line(1, expense=None, tax="5", code=vat())], status="SUBMITTED"),
        invoice([line(1, tax="5", code=TaxCodeFacts(ORG, "WITHHOLDING", PAID, None))], status="DRAFT"),
        invoice([line(1, tax="5", code=vat()), line(2, expense=None)], status="APPROVED"),
        invoice([line(1, tax="5", code=None)], status="DRAFT"),
        invoice([line(1, expense=CTRL)], status="DRAFT"),
    ]
    landed = 0
    for inv in cases:
        out = run(inv)
        if not out.lands:
            continue
        for ln in out.lines.values():
            landed += 1
            assert _line_ok_for_v1(ln.posting_account_ref, ln.tax_account_ref, ln.tax_amount), inv
    assert landed == 4


def test_only_never_posted_statuses_take_the_d_m2_coding():
    wht = TaxCodeFacts(ORG, "WITHHOLDING", PAID, None)
    for status in ("VOID", "REJECTED", "PAID"):
        out = run(invoice([line(1, tax="5", code=wht)], status=status))
        assert out.bucket == CARRY and out.lines[lid(1)].tax_amount == D(5)  # untouched (legacy classifier ref)
    posted = run(invoice([line(1, tax="5", code=wht)], ledger={EXP: D(105)}, status="DRAFT"))
    assert posted.lines[lid(1)].tax_amount == D(5)  # has ledger lines: legacy-posted, GL history, not D-M2


def test_rules_compose_on_one_invoice_with_one_bridge_line_each():
    wht = TaxCodeFacts(ORG, "WITHHOLDING", PAID, None)
    inv = invoice(
        [line(1, amount="100.004", expense=None), line(2, amount="50.00", tax="5", code=wht), line(3, amount="10")],
        status="SUBMITTED",
        sub="160.00",
        tax="5",
        total="165.00",
        items={lid(3): "ITEM-1"},
    )
    out = run(inv)
    assert out.bucket == TRANSFORM and out.lands
    assert rules(out) == sorted([T_EXP_FALLBACK, T_ROUND_2DP, T_ITEM_NONSTOCK, T_UNPOSTED_CORRECT])
    assert out.header["total_amount"] == D("160.00")


def test_negative_amount_invoices_are_never_transformed():
    out = run(invoice([line(1, amount="-40", expense=None)], status="DRAFT"))
    assert out.bucket == CARRY and out.bridge == ()


# ---- the bridge: legacy = carried + transformed + queued ----


def _mixed_outcomes():
    wht = TaxCodeFacts(ORG, "WITHHOLDING", PAID, None)
    invs = [
        invoice([line(1, amount="100")], n=1, status="SUBMITTED"),  # carry
        invoice([line(2, expense=None, amount="30")], n=2, status="DRAFT"),  # fallback
        invoice([line(3, amount="12.345")], n=3, status="DRAFT", sub="12.34", total="12.34"),  # round
        invoice([line(4, amount="200", tax="10", code=wht)], n=4, status="SUBMITTED", sub="200", tax="10", total="210"),
        invoice([line(5, expense=CTRL, amount="70")], n=5, status="DRAFT"),  # queued, not landed
        invoice([line(6, amount="9")], n=6, status="DRAFT", items={lid(6): "I"}),  # item
    ]
    return [run(i) for i in invs]


def test_every_invoice_is_in_exactly_one_bucket_and_the_partition_balances():
    ledger = TransformLedger(tuple(_mixed_outcomes()), FALLBACK)
    part = ledger.partition()
    b = part["buckets"]
    assert part["balanced"] is True
    assert (b[CARRY]["invoices"], b[TRANSFORM]["invoices"], b[QUEUE]["invoices"]) == (1, 4, 1)
    assert part["legacy"]["invoices"] == 6 and part["legacy"]["lines"] == 6
    legacy = D(part["legacy"]["total"])
    assert legacy == sum(D(b[k]["legacy_total"]) for k in b)
    # v2 side: landed total = landed legacy total + the itemised header deltas (WHT -10, rounding 12.345 -> 12.34)
    landed_legacy = legacy - D(b[QUEUE]["legacy_total"])
    assert D(part["v2_landed_total"]) == landed_legacy + D(part["header_delta_total"])
    assert D(part["header_delta_total"]) == D("-10")
    wht_rule = ledger.rules()[T_UNPOSTED_CORRECT]
    assert D(wht_rule["header"]["delta"]) == D("-10") and D(wht_rule["line"]["delta"]) == D("-10")


def test_every_rule_has_a_receipt_row_and_each_application_is_itemised():
    ledger = TransformLedger(tuple(_mixed_outcomes()), FALLBACK)
    rows = ledger.rules()
    assert set(rows) == set(RULES)
    assert rows[T_EXP_FALLBACK]["line"]["applications"] == 1 and rows[T_ITEM_NONSTOCK]["line"]["applications"] == 1
    assert rows[T_UNPOSTED_CORRECT]["header"]["applications"] == 1
    items = ledger.bridge_lines()
    assert all(
        {"rule", "scope", "invoice_id", "legacy_amount", "v2_amount", "delta", "fingerprint"} <= set(i) for i in items
    )
    assert sum(1 for i in items if i["rule"] == T_UNPOSTED_CORRECT) == 2  # the line and the header
    assert ledger.queue() == {"invoices": 1, "landed": 0, "by_reason": {"expense_account_is_control": 1}}


def test_ledger_is_deterministic_and_the_itemised_file_is_owner_only(tmp_path):
    a = TransformLedger(tuple(_mixed_outcomes()), FALLBACK)
    b = TransformLedger(tuple(_mixed_outcomes()), FALLBACK)
    assert a.itemised_sha256() == b.itemised_sha256()
    path = tmp_path / "r.json.transforms.json"
    a.write_itemised(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["fallback_account"] == FALLBACK


def test_parity_delta_counts_the_null_expense_lines_that_now_land():
    outs = [
        run(invoice([line(1, expense=None), line(2, expense=None)], n=1, status="SUBMITTED")),  # 2 fallback
        run(invoice([line(3, expense=None, tax="5", code=vat())], n=2, status="DRAFT", sub="100", total="105")),  # 1
        run(
            _a1_elsewhere([line(4, expense=None, tax="10", code=vat())], {ELSEWHERE: D(100), TAXACCT: D(10)}, n=3)
        ),  # legacy GL
        run(invoice([line(5, expense=None), line(6, expense=CTRL)], n=4, status="DRAFT")),  # queued: not landed
    ]
    parity = TransformLedger(tuple(outs), FALLBACK).parity()
    assert parity == {
        "null_expense_lines": 5,
        "null_expense_invoices": 4,
        "landed_on_fallback": 3,
        "landed_on_legacy_gl_account": 1,
        "not_landed_queued": 1,
        "declared_written_delta_lines": 4,
    }


# ---- the snapshot build (reads rows, refuses without the declared input) ----


def _rows():
    inv = uid(9001)
    return dict(
        rows=[
            dict(
                invoice_id=inv,
                organization_id=ORG,
                journal_entry_id=None,
                ap_control_account_id=None,
                subtotal="100",
                total_amount="100",
                line_id=lid(1),
                line_amount="100",
                tax_amount="0",
                expense_account_id=None,
                tax_code_id=None,
                code_row=None,
                code_organization_id=None,
                tax_type=None,
                tax_paid_account_id=None,
                paid_row=None,
                deferral_pair_account_id=None,
                deferral_row=None,
            )
        ],
        ledger_rows=[],
        global_accounts=[],
        deferral_rows=[],
        headers=[dict(invoice_id=inv, status="DRAFT", subtotal="100", tax_amount="0", total_amount="100")],
        items=[],
        account_rows=[
            dict(account_id=a, account_code=f.code, is_active=f.active, is_posting_allowed=f.postable)
            for a, f in ACCOUNTS.items()
        ],
    )


def test_build_refuses_before_any_outcome_without_the_fallback():
    with pytest.raises(TransformParamsError) as exc:
        build_transforms(params=TransformParams(None), **_rows())
    assert exc.value.code == "fallback_expense_account_missing"


def test_build_maps_a_line_and_its_header():
    build = build_transforms(params=TransformParams(FALLBACK), **_rows())
    (entry,) = build.line_map.values()
    assert entry["posting_account_ref"] == FALLBACK and entry["line_amount"] == D(100)
    (header,) = build.header_map.values()
    assert header == {"subtotal": "100", "tax_amount": "0", "total_amount": "100"}  # unchanged: raw values pass through
    assert build.ledger.parity()["declared_written_delta_lines"] == 1


def test_enrolment_maps_refuse_a_transform_map_without_params():
    src = sa.create_engine("sqlite://").connect()
    with pytest.raises(TransformParamsError):
        EnrolmentMaps(src, src).get("supplier_line_transforms")


# ---- the executor: lookups land the transformed values; a queued row is a named exception ----


def _specs():
    inv = next(p for p in WAVE3_PAIRINGS if p.destination == "mod_payables.supplier_invoices")
    lin = next(p for p in WAVE3_PAIRINGS if p.destination == "mod_payables.supplier_invoice_lines")
    nn = lambda *names: tuple(DestinationColumn(n, nullable=False, has_default=False) for n in names)  # noqa: E731
    header = build_spec(
        source_table="supplier_invoice",
        destination_table="supplier_invoices",
        source_columns=("invoice_id", "subtotal", "tax_amount", "total_amount"),
        destination_columns=nn("id", "subtotal", "tax_amount", "total_amount"),
        lookups={k: v for k, v in inv.lookups.items() if v[0] == "supplier_invoice_transforms"},
    )
    lines = build_spec(
        source_table="supplier_invoice_line",
        destination_table="supplier_invoice_lines",
        source_columns=("line_id", "invoice_id", "line_amount", "tax_amount", "expense_account_id"),
        destination_columns=(
            *nn("id", "invoice_id", "line_amount", "tax_amount", "posting_account_ref"),
            DestinationColumn("tax_account_ref", nullable=True, has_default=False),
        ),
        lookups={k: v for k, v in lin.lookups.items() if v[0] == "supplier_line_transforms"},
    )
    return header, lines


def test_executor_lands_transformed_values_and_names_the_queued_rows():
    header_spec, line_spec = _specs()
    assert "line_amount" not in line_spec.column_map and "posting_account_ref" not in line_spec.column_map
    good, bad = uid(9001), uid(9002)
    build = build_transforms(
        params=TransformParams(FALLBACK),
        rows=[
            dict(
                invoice_id=i,
                organization_id=ORG,
                journal_entry_id=None,
                ap_control_account_id=None,
                subtotal="30",
                total_amount="30",
                line_id=ln,
                line_amount="30",
                tax_amount="0",
                expense_account_id=acct,
                tax_code_id=None,
                code_row=None,
                code_organization_id=None,
                tax_type=None,
                tax_paid_account_id=None,
                paid_row=None,
                deferral_pair_account_id=None,
                deferral_row=None,
            )
            for i, ln, acct in ((good, lid(1), None), (bad, lid(2), CTRL))
        ],
        ledger_rows=[],
        global_accounts=[],
        deferral_rows=[],
        headers=[
            dict(invoice_id=i, status="DRAFT", subtotal="30", tax_amount="0", total_amount="30") for i in (good, bad)
        ],
        items=[],
        account_rows=_rows()["account_rows"],
    )
    maps = {"supplier_line_transforms": build.line_map, "supplier_invoice_transforms": build.header_map}
    src, dst = sa.create_engine("sqlite://"), sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE supplier_invoice (invoice_id TEXT, subtotal TEXT, tax_amount TEXT, total_amount TEXT)"
            )
        )
        c.execute(
            sa.text(
                "CREATE TABLE supplier_invoice_line (line_id TEXT, invoice_id TEXT, line_amount TEXT, "
                "tax_amount TEXT, expense_account_id TEXT)"
            )
        )
        for i in (good, bad):
            c.execute(sa.text("INSERT INTO supplier_invoice VALUES (:i, '30', '0', '30')"), dict(i=i))
        c.execute(sa.text("INSERT INTO supplier_invoice_line VALUES (:l, :i, '30', '0', NULL)"), dict(l=lid(1), i=good))
        c.execute(
            sa.text("INSERT INTO supplier_invoice_line VALUES (:l, :i, '30', '0', :a)"), dict(l=lid(2), i=bad, a=CTRL)
        )
    with dst.begin() as c:
        c.execute(
            sa.text("CREATE TABLE supplier_invoices (id TEXT, subtotal TEXT, tax_amount TEXT, total_amount TEXT)")
        )
        c.execute(
            sa.text(
                "CREATE TABLE supplier_invoice_lines (id TEXT, invoice_id TEXT, line_amount TEXT, tax_amount TEXT, "
                "posting_account_ref TEXT, tax_account_ref TEXT)"
            )
        )
    with src.connect() as s, dst.begin() as d:
        h = execute_transform(s, d, header_spec, id_maps=maps)
        ln = execute_transform(s, d, line_spec, id_maps=maps)
        rows = d.execute(sa.text("SELECT id, posting_account_ref, tax_account_ref FROM supplier_invoice_lines")).all()
        headers = d.execute(sa.text("SELECT id FROM supplier_invoices")).all()
    assert [r[1] for r in rows] == [FALLBACK] and rows[0][2] is None  # the NULL-expense line landed on the fallback
    assert (h.written, ln.written) == (1, 1)
    assert [(e.source_key, e.reason) for e in h.exceptions] == [(bad, "transform_queue:expense_account_is_control")]
    assert [(e.source_key, e.reason) for e in ln.exceptions] == [(lid(2), "transform_queue:expense_account_is_control")]
    assert [r[0] for r in headers] == [good]


def test_declared_role_codes_equal_ledger_roles():
    from erp_v2 import ledger_roles
    from erp_v2.importer import transforms

    assert transforms.CONTROL_ACCOUNT_CODES == ledger_roles.CONTROL_ACCOUNT_CODES
    assert transforms.INVOICE_TAX_ACCOUNT_CODES == ledger_roles.INVOICE_TAX_ACCOUNT_CODES
    assert transforms.INPUT_VAT_RELEASE_CODES == ledger_roles.INPUT_VAT_RELEASE_CODES


# ---- the operator CLI: refuse before any write without the declared input; receipt carries the bridge ----


def _cli_world(tmp_path, monkeypatch):
    from test_importer_cli import _manifest

    from erp_v2.importer import cli

    src_path, dst_path = tmp_path / "src.db", tmp_path / "dst.db"
    src, dst = sa.create_engine(f"sqlite:///{src_path}"), sa.create_engine(f"sqlite:///{dst_path}")
    good, queued = uid(9001), uid(9002)
    with src.begin() as c:
        for stmt in (
            "CREATE TABLE supplier_invoice (invoice_id TEXT, organization_id TEXT, journal_entry_id TEXT, "
            "ap_control_account_id TEXT, status TEXT, subtotal TEXT, tax_amount TEXT, total_amount TEXT)",
            "CREATE TABLE supplier_invoice_line (line_id TEXT, invoice_id TEXT, line_amount TEXT, tax_amount TEXT, "
            "expense_account_id TEXT, tax_code_id TEXT, item_id TEXT)",
            "CREATE TABLE tax_code (tax_code_id TEXT, organization_id TEXT, tax_type TEXT, tax_paid_account_id TEXT)",
            "CREATE TABLE account (account_id TEXT, account_code TEXT, deferral_pair_account_id TEXT, "
            "is_active INTEGER, is_posting_allowed INTEGER)",
            "CREATE TABLE posted_ledger_line (journal_entry_id TEXT, account_id TEXT, debit_amount TEXT, "
            "credit_amount TEXT)",
            "CREATE TABLE journal_entry (journal_entry_id TEXT, source_module TEXT, source_document_type TEXT, "
            "source_document_id TEXT, status TEXT)",
        ):
            c.execute(sa.text(stmt))
        for a, f in ACCOUNTS.items():
            c.execute(
                sa.text("INSERT INTO account VALUES (:a, :c, NULL, :ia, :ip)"),
                dict(a=a, c=f.code, ia=int(f.active), ip=int(f.postable)),
            )
        for i in (good, queued):
            c.execute(
                sa.text("INSERT INTO supplier_invoice VALUES (:i, :o, NULL, NULL, 'SUBMITTED', '30', '0', '30')"),
                dict(i=i, o=ORG),
            )
        c.execute(
            sa.text("INSERT INTO supplier_invoice_line VALUES (:l, :i, '30', '0', NULL, NULL, NULL)"),
            dict(l=lid(1), i=good),
        )
        c.execute(
            sa.text("INSERT INTO supplier_invoice_line VALUES (:l, :i, '30', '0', :a, NULL, NULL)"),
            dict(l=lid(2), i=queued, a=CTRL),
        )
    header_spec, line_spec = _specs()
    with dst.begin() as c:
        c.execute(
            sa.text("CREATE TABLE supplier_invoices (id TEXT, subtotal TEXT, tax_amount TEXT, total_amount TEXT)")
        )
        c.execute(
            sa.text(
                "CREATE TABLE supplier_invoice_lines (id TEXT, invoice_id TEXT, line_amount TEXT, tax_amount TEXT, "
                "posting_account_ref TEXT, tax_account_ref TEXT)"
            )
        )
    monkeypatch.setattr(cli, "_resolve_sources", lambda engine, pairings: ({"x"}, {}, {}, ()))
    monkeypatch.setattr(cli, "build_specs", lambda present, cols, dest: ((header_spec, line_spec), ()))
    monkeypatch.setenv("ERP_IMPORT_APPROVED", "1")
    monkeypatch.setenv("ERP_IMPORT_APPLY_OK", "1")
    monkeypatch.setenv("LEGACY_DATABASE_URL", f"sqlite:///{src_path}")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", f"sqlite:///{dst_path}")
    return dst, ["--manifest", str(_manifest(tmp_path))], good, queued


@pytest.mark.parametrize("apply", [False, True])
def test_cli_refuses_without_the_fallback_account_before_any_write(tmp_path, monkeypatch, capsys, apply):
    from erp_v2.importer.cli import main

    dst, base, *_ = _cli_world(tmp_path, monkeypatch)
    code = main(["execute", *base, "--receipt", str(tmp_path / "r.json"), *(["--apply"] if apply else [])])
    assert code == 2
    assert "fallback_expense_account_missing" in capsys.readouterr().err
    with dst.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM supplier_invoices")).scalar() == 0
        assert c.execute(sa.text("SELECT count(*) FROM supplier_invoice_lines")).scalar() == 0
    assert not (tmp_path / "r.json").exists()


def test_cli_refuses_an_unusable_fallback_account(tmp_path, monkeypatch, capsys):
    from erp_v2.importer.cli import main

    _dst, base, *_ = _cli_world(tmp_path, monkeypatch)
    params = tmp_path / "p.json"
    params.write_text(json.dumps({"import.fallback_expense_account": CTRL}))
    assert main(["execute", *base, "--receipt", str(tmp_path / "r.json"), "--import-params-json", str(params)]) == 2
    assert "fallback_expense_account_invalid:expense_account_is_control" in capsys.readouterr().err


def test_cli_apply_lands_the_fallback_line_queues_the_other_and_receipts_the_bridge(tmp_path, monkeypatch):
    from erp_v2.importer.cli import main

    dst, base, good, _queued = _cli_world(tmp_path, monkeypatch)
    params = tmp_path / "p.json"
    params.write_text(json.dumps({"import.fallback_expense_account": FALLBACK}))
    receipt = tmp_path / "r.json"
    code = main(["execute", *base, "--receipt", str(receipt), "--import-params-json", str(params), "--apply"])
    assert code == 3  # the queued invoice is an unresolved exception: never a complete receipt
    data = json.loads(receipt.read_text())
    t = data["transforms"]
    assert t["ran"] and t["partition"]["balanced"] is True
    assert t["queue"] == {"invoices": 1, "landed": 0, "by_reason": {"expense_account_is_control": 1}}
    assert t["parity"]["declared_written_delta_lines"] == 1 and t["parity"]["null_expense_lines"] == 1
    assert t["rules"][T_EXP_FALLBACK]["line"]["applications"] == 1
    assert {e["reason"] for e in data["exceptions"]} == {"transform_queue:expense_account_is_control"}
    side = tmp_path / t["itemised_file"]
    assert stat.S_IMODE(os.stat(side).st_mode) == 0o600
    assert json.loads(side.read_text())["bridge_lines"][0]["rule"] == T_EXP_FALLBACK
    with dst.connect() as c:
        assert [r[0] for r in c.execute(sa.text("SELECT posting_account_ref FROM supplier_invoice_lines"))] == [
            FALLBACK
        ]
        assert [r[0] for r in c.execute(sa.text("SELECT id FROM supplier_invoices"))] == [good]
