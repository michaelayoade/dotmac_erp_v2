"""B-15 rev 2 units: cash-basis VAT arithmetic, the invoice and void journals, the opening derivation."""

import random
from decimal import Decimal
from uuid import uuid4

import pytest

from erp_v2.billing_gl import rules, vat, vat_opening
from erp_v2.billing_gl.rules import Disp, Effect, Env, FactView, Line, Plan

D = Decimal
CUR, DEF, REV, REC = (str(uuid4()) for _ in range(4))


def issued(total="120.00", tax="20.00", treatments=None):
    taxes = treatments or {"standard": (D(tax), DEF, CUR)}
    return vat.issue(uuid4(), D(total), taxes)


def balances(doc: vat.DocVat) -> dict[str, Decimal]:
    return {DEF: sum((p.remaining for p in doc.positions.values()), D(0))}


# ------------------------------------------------------------------ the journals


def test_invoice_defers_the_tax_and_opens_the_document() -> None:
    doc, moves = issued()
    assert doc.open == D("120.00") and doc.positions["standard"].remaining == D("20.00")
    assert vat.lines(moves, doc) == [(DEF, D("20.00"))]  # Cr 1455 20; revenue and 1400 are the plan's lines


def test_zero_tax_invoice_has_no_position() -> None:
    doc, moves = vat.issue(uuid4(), D("50.00"), {"standard": (D(0), DEF, CUR)})
    assert doc.positions == {} and moves == ()


def test_three_partial_payments_release_exactly_d() -> None:
    doc, _ = issued()
    released = D(0)
    for amount in ("50.00", "40.00", "30.00"):
        moves = vat.release(doc, D(amount), f"a-{amount}")
        vat.apply(doc, moves)
        released += -sum((m.deferral_delta for m in moves), D(0))
    assert released == D("20.00") and doc.open == 0 and doc.positions["standard"].remaining == 0
    # 8.33 + 6.67 and the closing payment takes exactly the remainder 5.00.


def test_half_even_rounding_and_the_closing_payment_absorbs_the_residue() -> None:
    doc, _ = issued(total="100.01", tax="0.01")
    first = vat.release(doc, D("50.00"), "a1")  # 50 x 0.01 / 100.01 = 0.0049995 -> 0.00
    assert first[0].deferral_delta == 0
    vat.apply(doc, first)
    last = vat.release(doc, D("50.01"), "a2")  # closes: takes all of R
    assert last[0].deferral_delta == D("-0.01")


def test_release_beyond_open_is_refused() -> None:
    doc, _ = issued()
    with pytest.raises(vat.VatRefused) as refused:
        vat.release(doc, D("120.01"), "a")
    assert refused.value.code == "vat_invariant"


# ---------------------------------------------------------------------- credit


def test_credit_split_when_tax_exceeds_remaining() -> None:
    doc, _ = issued(total="120.00", tax="20.00")
    vat.apply(doc, vat.release(doc, D("108.00"), "a"))  # R = 20 - 18 = 2.00; open 12.00
    assert doc.positions["standard"].remaining == D("2.00")
    moves = vat.credit(doc, {"standard": D("5.00")}, D("12.00"), "closeout:f")  # tax 5 > R 2
    reduce = next(m for m in moves if m.kind == "credit_reduce")
    current = next(m for m in moves if m.kind == "credit_current")
    assert reduce.deferral_delta == D("-2.00") and current.current_delta == D("-3.00")
    assert vat.lines(moves, doc) == sorted([(DEF, D("-2.00")), (CUR, D("-3.00"))])


def test_credit_that_closes_the_document_closes_out_the_remaining_deferral() -> None:
    """D-A6-9: when the credit brings open to 0 and deferral is left, Dr 1455 / Cr 1440 for exactly that."""
    doc, _ = issued(total="120.00", tax="20.00")
    vat.apply(doc, vat.release(doc, D("90.00"), "a"))  # R = 5.00
    moves = vat.credit(doc, {"standard": D("1.00")}, D("30.00"), "closeout:f")  # tax credited 1 < R 5
    vat.apply(doc, moves)
    assert doc.open == 0 and doc.positions["standard"].remaining == 0
    close = [m for m in moves if m.kind == "closeout"]
    assert close and close[0].deferral_delta == D("-4.00") and close[0].current_delta == D("4.00")


def test_credit_on_a_treatment_without_a_position_is_refused() -> None:
    doc, _ = issued()
    with pytest.raises(vat.VatRefused) as refused:
        vat.credit(doc, {"zero": D("1.00")}, D("10.00"), "closeout:f")
    assert refused.value.code == "vat_position_missing"


# ------------------------------------------------------------------ deallocation


def test_full_deallocation_unreleases_exactly_that_allocations_net_release() -> None:
    doc, _ = issued()
    for ref, amount in (("a1", "50.00"), ("a2", "40.00")):
        vat.apply(doc, vat.release(doc, D(amount), ref))
    before = doc.positions["standard"].remaining
    moves = vat.unrelease(doc, D("40.00"), "a2")
    assert moves[0].deferral_delta == D("6.67")  # exactly a2's release
    vat.apply(doc, moves)
    assert doc.positions["standard"].remaining == before + D("6.67") and doc.open == D("70.00")


def test_partial_deallocation_uses_the_formula_capped_at_the_net() -> None:
    doc, _ = issued()
    vat.apply(doc, vat.release(doc, D("60.00"), "a1"))  # releases 10.00
    moves = vat.unrelease(doc, D("30.00"), "a1")  # 30 x 20 / 120 = 5.00 <= 10.00
    assert moves[0].deferral_delta == D("5.00")
    vat.apply(doc, moves)
    assert vat.unrelease(doc, D("30.00"), "a1")[0].deferral_delta == D("5.00")  # the rest, now the full remainder


def test_deallocating_a_closed_document_reverses_a_credit_closeout_first() -> None:
    doc, _ = issued()
    vat.apply(doc, vat.release(doc, D("90.00"), "a"))
    vat.apply(doc, vat.credit(doc, {"standard": D("1.00")}, D("30.00"), "closeout:f"))
    moves = vat.unrelease(doc, D("90.00"), "a")
    assert moves[0].kind == "uncloseout"
    vat.apply(doc, moves)
    assert doc.open == D("90.00") and doc.positions["standard"].remaining <= doc.positions["standard"].deferred


def test_unknown_allocation_and_over_deallocation_are_refused() -> None:
    doc, _ = issued()
    vat.apply(doc, vat.release(doc, D("10.00"), "a"))
    with pytest.raises(vat.VatRefused) as unknown:
        vat.unrelease(doc, D("1.00"), "nope")
    assert unknown.value.code == "vat_allocation_unresolved"
    with pytest.raises(vat.VatRefused) as over:
        vat.unrelease(doc, D("10.01"), "a")
    assert over.value.code == "vat_invariant"


# ------------------------------------------------------------------------ void


def test_void_mirror_is_the_exact_negation() -> None:
    doc, moves = issued()
    mirrored = vat.negate(moves)
    assert [(m.deferral_delta, m.current_delta) for m in mirrored] == [
        (-m.deferral_delta, -m.current_delta) for m in moves
    ]
    vat.apply(doc, mirrored)
    assert doc.positions["standard"].remaining == 0
    assert vat.lines(mirrored, doc) == [(DEF, D("-20.00"))]


# ---------------------------------------------------------------- dispatch


def env_with(plan, **kw):
    base = {
        "functional_currency": "NGN",
        "receivable_account": REC,
        "prepayment_account": str(uuid4()),
        "prepaid_funding_account": None,
        "cash_accounts": {},
        "plan": plan,
    }
    return Env(**{**base, **kw})


def view(kind, effects=(), **kw):
    return FactView(
        version=1, kind=kind, fact_id=uuid4(), billing_account_id=uuid4(), currency="NGN", occurred_at=None,
        effects=tuple(Effect(lane, D(d)) for lane, d in effects), **kw,
    )  # fmt: skip


def test_invoice_posts_the_plan_and_blocks_with_its_reason() -> None:
    lines = (Line(REC, D(120), D(0), "r"), Line(REV, D(0), D(100), "rev"), Line(DEF, D(0), D(20), "vat"))
    rule = rules.rule_for(1, "invoice_issued")
    ok = rules.decide(rule, view("invoice_issued"), env_with(Plan(lines)))
    assert ok.disposition == Disp.POST and ok.lines == lines
    for reason in ("revenue_account_unmapped", "tax_account_unmapped", "line_accounts_mismatch"):
        blocked = rules.decide(rule, view("invoice_issued"), env_with(Plan(block=reason)))
        assert (blocked.disposition, blocked.reason) == (Disp.BLOCKED, reason)
    unbalanced = rules.decide(rule, view("invoice_issued"), env_with(Plan((Line(REC, D(5), D(0), "r"),))))
    assert unbalanced.reason == "lane_effects_unbalanced"


def test_void_of_a_document_is_the_plan_only_no_lane_lines() -> None:
    lines = (Line(REC, D(0), D(120), "r"), Line(REV, D(100), D(0), "rev"), Line(DEF, D(20), D(0), "vat"))
    rule = rules.rule_for(1, "reversal")
    decision = rules.decide(
        rule, view("reversal", [("receivable", "-120")]), env_with(Plan(lines), reversed_kind="invoice_issued")
    )
    assert decision.disposition == Disp.POST and decision.lines == lines


def test_allocation_adds_the_vat_release_to_the_lane_lines() -> None:
    prepay = str(uuid4())
    vat_lines = (Line(DEF, D("8.33"), D(0), "rel"), Line(CUR, D(0), D("8.33"), "rel"))
    decision = rules.decide(
        rules.rule_for(1, "allocation"),
        view("allocation", [("available_credit", "-50"), ("receivable", "-50")]),
        env_with(Plan(vat_lines), prepayment_account=prepay),
    )
    assert decision.disposition == Disp.POST and len(decision.lines) == 4
    assert sum((ln.debit - ln.credit for ln in decision.lines), D(0)) == 0


def test_reallocation_with_no_lane_effects_posts_the_vat_only() -> None:
    vat_lines = (Line(DEF, D("3.00"), D(0), "rel"), Line(CUR, D(0), D("3.00"), "rel"))
    decision = rules.decide(rules.rule_for(1, "reallocation"), view("reallocation"), env_with(Plan(vat_lines)))
    assert decision.disposition == Disp.POST and decision.lines == vat_lines
    assert (
        rules.decide(rules.rule_for(1, "reallocation"), view("reallocation"), env_with(Plan())).reason == "lane_neutral"
    )


def test_vat_block_reason_blocks_the_fact() -> None:
    decision = rules.decide(
        rules.rule_for(1, "allocation"),
        view("allocation", [("available_credit", "-5"), ("receivable", "-5")]),
        env_with(Plan(block="vat_position_missing")),
    )
    assert (decision.disposition, decision.reason) == (Disp.BLOCKED, "vat_position_missing")


# --------------------------------------------------------------- random lifecycle


@pytest.mark.parametrize("seed", range(40))
def test_random_lifecycle_keeps_the_invariants_and_every_tie(seed) -> None:
    """Issue / pay / credit / deallocate / void in random order: 4.4 holds after every fact and the GL accounts
    (1455 = sum R, 1440 = what was released net of credits) equal the position state exactly."""
    rng = random.Random(seed)  # noqa: S311  (a deterministic test seed, not security)
    total = D(rng.choice(["120.00", "1075.50", "333.33", "10.07"]))
    tax = (total * D(rng.choice(["0.05", "0.075", "0.2"]))).quantize(D("0.01"))
    doc, moves = vat.issue(uuid4(), total, {"standard": (tax, DEF, CUR)})
    ledger = {DEF: D(0), CUR: D(0)}
    folded: list[vat.Move] = []
    credited = D(0)

    def post(batch):
        for account, amount in vat.lines(tuple(batch), doc):
            ledger[account] += amount
        folded.extend(batch)

    post(moves)
    refs: list[str] = []
    voided = False
    for step in range(rng.randint(3, 12)):
        action = rng.choice(["pay", "pay", "credit", "dealloc", "void"])
        tax_credited = D(0)
        try:
            if action == "pay" and doc.open > 0:
                amount = (doc.open * D(rng.choice(["0.1", "0.33", "0.5", "1"]))).quantize(D("0.01"))
                if amount <= 0:
                    continue
                batch = vat.release(doc, amount, f"a{step}")
                refs.append(f"a{step}")
            elif action == "credit" and doc.open > 0:
                part = (doc.open * D(rng.choice(["0.2", "0.5", "1"]))).quantize(D("0.01"))
                if part <= 0:
                    continue
                tax_credited = min(part, (part * tax / total).quantize(D("0.01")))
                batch = vat.credit(doc, {"standard": tax_credited}, part, f"closeout:c{step}")
            elif action == "dealloc" and refs:
                ref = refs.pop(rng.randrange(len(refs)))
                batch = vat.unrelease(doc, doc.settled[ref], ref)
            elif action == "void" and not refs and doc.open == doc.total and credited == 0 and not voided:
                batch = vat.negate(tuple(folded))
                voided = True
            else:
                continue
            vat.apply(doc, batch)
            post(batch)
            credited += tax_credited
        except vat.VatRefused:
            continue
        pos = doc.positions["standard"]
        assert 0 <= pos.remaining <= pos.deferred
        assert 0 <= doc.open <= doc.total
        assert doc.open != 0 or pos.remaining == 0
        assert ledger[DEF] == pos.remaining  # GL 1455 (Cr) equals R exactly
        # Releases and close-outs only move value from 1455 to 1440, so the two together are the tax deferred at
        # issue less the tax credited (and nothing at all once voided).
        assert ledger[DEF] + ledger[CUR] == (D(0) if voided else tax - credited)
        if voided:
            break


# ------------------------------------------------------------------ opening load


def _item(**kw):
    base = {
        "document_id": str(uuid4()),
        "treatment": "standard",
        "total": "120.00",
        "tax": "20.00",
        "open_initial": "60.00",
        "journal_1455_credit": "20.00",
        "reclass": ["10.00"],
        "deferral_account": DEF,
        "current_account": CUR,
        "legacy_ref": "INV-1",
    }
    return {**base, **kw}


def _parse(items):
    import json

    return vat_opening.parse(json.dumps({"version": 1, "tenant_id": str(uuid4()), "items": items}).encode())


def test_opening_remaining_is_the_legacy_credit_less_the_attributed_reclass() -> None:
    parsed = _parse([_item()])
    assert vat_opening.derive(parsed["items"][0]) == (D("10.00"), None)


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"reclass": ["25.00"]}, "negative_remaining"),
        ({"journal_1455_credit": "25.00", "reclass": []}, "remaining_above_tax"),
        ({"reclass": None}, "unattributable"),
    ],
)
def test_unloadable_opening_items_become_e38_items_not_positions(override, reason) -> None:
    parsed = _parse([_item(**override), _item()])
    planned = vat_opening.plan(parsed, "a" * 64)
    assert planned["counts"] == {"load": 1, "e38": 1} and planned["e38"][0]["reason"] == reason


def test_opening_plan_sha_is_stable_and_input_bound() -> None:
    items = [_item(document_id=str(uuid4()))]
    body = {"version": 1, "tenant_id": str(uuid4()), "items": items}
    import json

    raw = json.dumps(body).encode()
    first = vat_opening.plan(vat_opening.parse(raw), "b" * 64)
    assert first["plan_sha256"] == vat_opening.plan(vat_opening.parse(raw), "b" * 64)["plan_sha256"]
    assert first["plan_sha256"] != vat_opening.plan(vat_opening.parse(raw), "c" * 64)["plan_sha256"]


def test_opening_input_refuses_duplicates_unknown_keys_and_bad_amounts() -> None:
    from erp_v2.migration_ledger.common import LedgerRefused

    dup = _item()
    for bad in ([dup, dup], [{**_item(), "extra": 1}], [_item(open_initial="121.00")]):
        with pytest.raises(LedgerRefused):
            _parse(bad)


def test_vat_reasons_are_gate_inputs() -> None:
    from erp_v2.billing_gl import checks

    assert checks.tax_accounts_mapped({"standard": {"current": CUR, "deferral": DEF}})
    assert not checks.tax_accounts_mapped({}) and not checks.tax_accounts_mapped({"standard": {"current": CUR}})
