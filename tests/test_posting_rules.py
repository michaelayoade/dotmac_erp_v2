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
    assert refusal(consequence(kind="supplier_payout")) == "unknown_consequence_kind"
    assert refusal(consequence(owner="payroll", kind="liability_settlement")) == "unknown_consequence_kind"


def test_supplier_payment_kinds_are_declared_with_uuid_only_refs():
    for kind in ("supplier_payment", "supplier_payment_reversal"):
        policy = rules.policy_for("payables", kind)
        assert policy.code_refs == frozenset()
        assert rules.validate(consequence(kind=kind)) == policy
        with pytest.raises(PostingRefused) as exc:
            rules.classify_ref(policy, "2000")  # a code is not a declared form for these kinds
        assert exc.value.code == "account_ref_form_refused"


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


def test_s2_native_shape_is_refused_not_crashed():
    # The pre-fix-up Starter payroll shape (source_type/lines/content_sha256) is not a consequence.
    raw = SimpleNamespace(
        tenant_id="t",
        source_type="payroll.run.finalized",
        source_ref="r",
        effective_date=date(2026, 1, 1),
        lines=(),
        content_sha256=FP,
    )
    assert refusal(raw) == "invalid_consequence"


def test_payroll_consequence_shape_validates_with_declared_code_form():
    c = consequence((entry(A1, debit="100"), entry("2040", credit="100")), owner="payroll", kind="payroll_run")
    assert rules.validate(c).code_refs == frozenset({"2040"})
    settle = consequence(
        (entry("2040", debit="40"), entry(A2, credit="40")), owner="payroll", kind="payroll_liability_settlement"
    )
    rules.validate(settle)


def test_policies_are_read_only():
    with pytest.raises(TypeError):
        rules.POLICIES[("x", "y")] = rules.RefPolicy()  # type: ignore[index]


def test_identity_is_deterministic_and_bounded():
    c = consequence()
    assert rules.idempotency_key(c.source, "post") == "erp.post:payables:supplier_invoice:doc-1:1:post"
    assert rules.journal_number(c.source) == rules.journal_number(consequence().source)
    assert len(rules.journal_number(c.source)) <= 50
    assert rules.MAX_KEY_LENGTH == 200
    c.source.document_id = "x" * 300
    with pytest.raises(PostingRefused) as exc:
        rules.idempotency_key(c.source, "create")
    assert exc.value.code == "identity_too_long"
