"""Payroll formula conversion: exact subset only, rest unconvertible."""

from __future__ import annotations

from decimal import Decimal

from erp_v2.importer.payroll_formula import convert_rule

ABBR = {"BASIC", "HRA", "TRANSPORT"}


def _c(**kw):
    base = {"amount": None, "amount_based_on_formula": True, "formula": None, "condition": None, "known_abbrs": ABBR}
    base.update(kw)
    return convert_rule(**base)


def test_fixed_and_input() -> None:
    assert _c(amount_based_on_formula=False, amount=Decimal(500)).method == "fixed"
    assert _c(amount_based_on_formula=False, amount=None).method == "input"
    assert _c(amount_based_on_formula=0, amount=Decimal(0)).method == "fixed"


def test_percentage_patterns() -> None:
    conv = _c(formula="BASIC * 0.25")
    assert (conv.method, conv.rate, conv.basis_abbrs) == ("percentage", Decimal("0.25"), ("BASIC",))
    conv = _c(formula="(BASIC + HRA) * 10 / 100")
    assert conv.rate == Decimal("0.1") and conv.basis_abbrs == ("BASIC", "HRA")
    conv = _c(formula="0.05 * BASIC")
    assert conv.rate == Decimal("0.05")
    conv = _c(formula="BASIC")
    assert conv.rate == Decimal(1)


def test_unconvertible_is_none_not_approximate() -> None:
    assert _c(formula="BASIC - HRA * 0.1") is None
    assert _c(formula="min(BASIC, 1000)") is None
    assert _c(formula="UNKNOWN * 0.1") is None
    assert _c(formula="BASIC * 0.1 if BASIC > 0") is None
    assert _c(formula="BASIC * 0.1", condition="doc.status == 1") is None
    assert _c(formula="") is None
    assert _c(formula=None) is None
