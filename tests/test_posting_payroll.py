"""Payroll adapter with an object carrying EXACTLY the Starter payroll a3 consequence fields."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from erp_v2.posting import payroll, rules
from erp_v2.posting.rules import PostingRefused


# Field lists copied from dotmac_payroll.consequences at Starter 7d9ac07b (no exchange_rate).
@dataclass(frozen=True, slots=True)
class Source:
    owner: str
    document_kind: str
    document_id: str
    version: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Entry:
    account_ref: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    description: str | None = None
    dimension_refs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Consequence:
    source: Source
    posting_date: date
    description: str
    currency_code: str
    minor_units: int
    entries: tuple[Entry, ...]
    fingerprint: str
    idempotency_key: str


FP = "a" * 64
A1 = str(UUID(int=1))


def real(currency="NGN", kind="payroll_run"):
    return Consequence(
        Source("payroll", kind, "run-1", "1", FP),
        date(2026, 1, 31),
        "Payroll run",
        currency,
        2,
        (Entry(A1, debit=Decimal("100.00")), Entry("2040", credit=Decimal("100.00"))),
        FP,
        "payroll.run.finalized:run-1",
    )


def test_exact_payroll_shape_adapts_and_validates():
    adapted = payroll.to_bridge_input(real(), functional_currency="NGN")
    assert adapted.exchange_rate == Decimal(1) and adapted.currency_code == "NGN"
    assert rules.validate(adapted).code_refs == frozenset({"2040"})
    settle = payroll.to_bridge_input(real(kind="payroll_liability_settlement"), functional_currency="ngn")
    rules.validate(settle)


def test_the_raw_payroll_object_alone_is_refused_by_the_bridge_rules():
    with pytest.raises(PostingRefused) as exc:
        rules.validate(real())
    assert exc.value.code == "invalid_consequence"


def test_non_functional_currency_refuses():
    with pytest.raises(PostingRefused) as exc:
        payroll.to_bridge_input(real(currency="USD"), functional_currency="NGN")
    assert exc.value.code == "non_functional_currency"


@pytest.mark.parametrize("bad", ["", "NG", None])
def test_functional_currency_must_be_supplied(bad):
    with pytest.raises(PostingRefused) as exc:
        payroll.to_bridge_input(real(), functional_currency=bad)
    assert exc.value.code == "functional_currency_required"


def test_other_shapes_refuse():
    with pytest.raises(PostingRefused) as exc:
        payroll.to_bridge_input(object(), functional_currency="NGN")
    assert exc.value.code == "invalid_consequence"
