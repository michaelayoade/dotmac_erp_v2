"""Pure unit tests for B2: form parsing, currency setting declaration, no hard-coded currency."""

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from erp_v2.journals import forms, policy
from erp_v2.journals.service import JournalRefused

JOURNALS = Path(__file__).resolve().parents[1] / "src" / "erp_v2" / "journals"


def values(**extra):
    base = {
        "request_id": str(uuid4()),
        "posting_date": "2026-01-31",
        "kind": "STANDARD",
        "description": "d",
        "evidence_ref": "SUB-1",
        "account_1": str(uuid4()),
        "debit_1": "10",
        "account_2": str(uuid4()),
        "credit_2": "10",
    }
    base.update(extra)
    return base


def test_parse_skips_blank_rows_and_keeps_decimals_exact():
    command = forms.parse_prepare(values(debit_1="10.123456", credit_2="10.123456", account_3="", debit_3=""))
    assert [(line.debit, line.credit) for line in command.lines] == [
        (Decimal("10.123456"), Decimal(0)),
        (Decimal(0), Decimal("10.123456")),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"request_id": "nope"},
        {"posting_date": "31/01/2026"},
        {"kind": "bogus"},
        {"debit_1": "abc"},
        {"debit_1": "NaN"},
        {"debit_1": "Infinity"},
        {"account_1": "not-a-uuid"},
    ],
)
def test_parse_refuses_malformed_input(bad):
    with pytest.raises(JournalRefused):
        forms.parse_prepare(values(**bad))


def test_functional_currency_setting_is_tenant_scoped_without_fallback_or_default():
    spec = policy.FUNCTIONAL_CURRENCY_SPEC
    assert (spec.required_at, spec.inherits, spec.default) == ("tenant", False, None)
    assert str(spec.domain) == "accounting_policy" and spec.key == "functional_currency"
    spec.validator("NGN")
    for bad in ("ngn", "NG", "NGNN", "N1N", 5):
        with pytest.raises(ValueError):
            spec.validator(bad)


def test_no_currency_literal_in_journal_code_or_templates():
    offenders = []
    for path in [*JOURNALS.glob("*.py"), *(JOURNALS.parent / "templates" / "journals").glob("*.html")]:
        text = path.read_text()
        for literal in ("NGN", "USD", "GHS", "EUR"):
            if literal in text:
                offenders.append((path.name, literal))
    assert offenders == []
