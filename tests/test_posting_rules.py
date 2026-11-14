"""Pure posting rules: refusals and identity. No database or Starter packages needed."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from erp_v2.posting import rules
from erp_v2.posting.rules import PostingRefused

A1, A2 = str(UUID(int=1)), str(UUID(int=2))
FP = "a" * 64
LETTERS = str(UUID(int=0xABCDEF))


def entry(ref, debit="0", credit="0", dims=()):
    return SimpleNamespace(
        account_ref=ref, debit=Decimal(debit), credit=Decimal(credit), description="x", dimension_refs=dims
    )


def consequence(entries=None, owner="payables", kind="supplier_invoice", **over):
    base = dict(
        source=SimpleNamespace(owner=owner, document_kind=kind, document_id="doc-1", version="1", fingerprint=FP),
        posting_date=date(2026, 1, 5),
        description="Supplier invoice 1",
        currency_code="NGN",
        exchange_rate=Decimal("1"),
        entries=entries if entries is not None else (entry(A1, debit="10"), entry(A2, credit="10")),
        fingerprint=FP,
    )
    base.update(over)
    return SimpleNamespace(**base)


def refusal(c):
    with pytest.raises(PostingRefused) as exc:
        rules.validate(c)
    return exc.value.code


def test_balanced_consequence_validates():
    assert rules.validate(consequence()) == rules.POLICIES[("payables", "supplier_invoice")]


def test_unbalanced_refuses():
    assert refusal(consequence((entry(A1, debit="10"), entry(A2, credit="9")))) == "unbalanced"


def test_single_entry_and_zero_total_refuse():
    assert refusal(consequence((entry(A1, debit="10"),))) == "unbalanced"
    assert refusal(consequence((entry(A1), entry(A2)))) == "invalid_amount"


def test_both_sides_negative_and_scale_refuse():
    assert refusal(consequence((entry(A1, debit="1", credit="1"), entry(A2, credit="1")))) == "invalid_amount"
    assert refusal(consequence((entry(A1, debit="-1"), entry(A2, credit="1")))) == "invalid_amount"
    assert refusal(consequence((entry(A1, debit="1.0000001"), entry(A2, credit="1.0000001")))) == "invalid_amount"
    assert refusal(consequence((entry(A1, debit="NaN"), entry(A2, credit="1")))) == "invalid_amount"


def test_unknown_owner_kind_refuses():
    assert refusal(consequence(owner="mystery")) == "unknown_consequence_kind"
    assert refusal(consequence(kind="supplier_payment")) == "unknown_consequence_kind"


def test_account_ref_forms_one_declared_form_only():
    payables = rules.policy_for("payables", "supplier_invoice")
    payroll = rules.policy_for("payroll", "payroll_run")
    assert rules.classify_ref(payables, A1) == ("uuid", A1)
    assert rules.classify_ref(payroll, "2040") == ("code", "2040")
    assert rules.classify_ref(payroll, A1) == ("uuid", A1)  # mixed refs: UUID + declared code
    for bad in ("2040", "2041", LETTERS.upper(), A1.replace("-", ""), "", " " + A1, None, 2040):
        with pytest.raises(PostingRefused) as exc:
            rules.classify_ref(payables, bad)
        assert exc.value.code == "account_ref_form_refused"
    with pytest.raises(PostingRefused):
        rules.classify_ref(payroll, "2041")


def test_undeclared_code_in_consequence_refuses():
    assert refusal(consequence((entry("2040", debit="1"), entry(A2, credit="1")))) == "account_ref_form_refused"


def test_dimensions_are_refused_not_ignored():
    dims = (("DEPT", "ops"),)
    assert refusal(consequence((entry(A1, debit="1", dims=dims), entry(A2, credit="1")))) == "dimensions_unsupported"


@pytest.mark.parametrize(
    "over",
    [
        {"currency_code": "naira"},
        {"exchange_rate": Decimal("0")},
        {"exchange_rate": 1},
        {"fingerprint": "xyz"},
        {"description": " "},
        {"posting_date": "2026-01-05"},
    ],
)
def test_malformed_header_refuses(over):
    assert refusal(consequence(**over)) == "invalid_consequence"


def test_identity_is_deterministic_and_bounded():
    c = consequence()
    assert rules.idempotency_key(c.source, "post") == "erp.post:payables:supplier_invoice:doc-1:1:post"
    assert rules.journal_number(c.source) == rules.journal_number(consequence().source)
    assert len(rules.journal_number(c.source)) <= 50
    c.source.document_id = "x" * 300
    with pytest.raises(PostingRefused) as exc:
        rules.idempotency_key(c.source, "create")
    assert exc.value.code == "identity_too_long"
