"""Pure, dependency-free posting rules: shape, balance, account-ref forms, identity.

Everything here refuses (``PostingRefused``) rather than repairs: no rounding,
no defaulting, no coercion of an unknown account reference into a guess.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

QUANTUM: Final = Decimal("0.000001")
MAX_KEY_LENGTH: Final = 240
_FINGERPRINT: Final = re.compile(r"[0-9a-f]{64}")


class PostingRefused(ValueError):
    """Fail-closed refusal with a stable machine-readable ``code``; nothing was written."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class _Source(Protocol):
    owner: str
    document_kind: str
    document_id: str
    version: str
    fingerprint: str


class _Entry(Protocol):
    account_ref: str
    debit: Decimal
    credit: Decimal
    description: str | None
    dimension_refs: tuple[tuple[str, str], ...]


class ConsequenceLike(Protocol):
    """Structural shape of a Starter owner module's ``AccountingConsequence``."""

    source: _Source
    posting_date: date
    description: str
    currency_code: str
    exchange_rate: Decimal
    entries: Sequence[_Entry]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RefPolicy:
    """Declared account-reference forms for one owner document kind.

    Every ref is an Account UUID unless its exact value is in ``code_refs``.
    """

    code_refs: frozenset[str] = frozenset()


# One declared form per owner field; later PRs append entries (reviewed).
POLICIES: Final[dict[tuple[str, str], RefPolicy]] = {
    ("payables", "supplier_invoice"): RefPolicy(),
    ("payables", "credit_note"): RefPolicy(),
    # payroll revision net_payable_account_ref is the account CODE (minting.py).
    ("payroll", "payroll_run"): RefPolicy(frozenset({"2040"})),
    ("payroll", "liability_settlement"): RefPolicy(frozenset({"2040"})),
}


def policy_for(owner: str, kind: str) -> RefPolicy:
    try:
        return POLICIES[(owner, kind)]
    except KeyError:
        raise PostingRefused("unknown_consequence_kind", f"{owner}/{kind}") from None


def classify_ref(policy: RefPolicy, ref: object) -> tuple[str, str]:
    """Return ("uuid", canonical) or ("code", code); anything else refuses."""
    if not isinstance(ref, str) or not ref:
        raise PostingRefused("account_ref_form_refused", "empty or non-text reference")
    if ref in policy.code_refs:
        return "code", ref
    try:
        if str(UUID(ref)) == ref:
            return "uuid", ref
    except ValueError:
        pass
    raise PostingRefused("account_ref_form_refused", "reference is neither a canonical account id nor a declared code")


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise PostingRefused("invalid_consequence", label)
    return value


def validate(consequence: ConsequenceLike) -> RefPolicy:
    """Shape + balance checks; returns the owner's ref policy. No database."""
    source = consequence.source
    owner = _text(source.owner, "source owner", 120)
    kind = _text(source.document_kind, "source document kind", 80)
    _text(source.document_id, "source document id", 120)
    _text(source.version, "source version", 60)
    for value, label in (
        (source.fingerprint, "source fingerprint"),
        (consequence.fingerprint, "consequence fingerprint"),
    ):
        if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
            raise PostingRefused("invalid_consequence", label)
    policy = policy_for(owner, kind)
    if not isinstance(consequence.posting_date, date):
        raise PostingRefused("invalid_consequence", "posting date")
    _text(consequence.description, "description", 2000)
    currency = consequence.currency_code
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise PostingRefused("invalid_consequence", "currency")
    rate = consequence.exchange_rate
    if not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0:
        raise PostingRefused("invalid_consequence", "exchange rate")
    entries = tuple(consequence.entries)
    if len(entries) < 2:
        raise PostingRefused("unbalanced", "fewer than two entries")
    debit = credit = Decimal(0)
    for entry in entries:
        if entry.dimension_refs:
            raise PostingRefused("dimensions_unsupported", "dimension refs are not mapped in this release")
        for amount in (entry.debit, entry.credit):
            if (
                not isinstance(amount, Decimal)
                or not amount.is_finite()
                or amount < 0
                or amount != amount.quantize(QUANTUM)
            ):
                raise PostingRefused("invalid_amount", "amounts must be finite, non-negative, at most six decimals")
        if (entry.debit > 0) == (entry.credit > 0):
            raise PostingRefused("invalid_amount", "each entry must carry exactly one side")
        classify_ref(policy, entry.account_ref)
        debit += entry.debit
        credit += entry.credit
    if debit != credit or debit <= 0:
        raise PostingRefused("unbalanced", "total debit differs from total credit")
    return policy


def source_key(source: _Source) -> str:
    return f"{source.owner}:{source.document_kind}:{source.document_id}:{source.version}"


def idempotency_key(source: _Source, step: str) -> str:
    key = f"erp.post:{source_key(source)}:{step}"
    if len(key) > MAX_KEY_LENGTH:
        raise PostingRefused("identity_too_long", "source identity exceeds the idempotency key limit")
    return key


def journal_number(source: _Source) -> str:
    """Deterministic, tenant-unique-by-construction journal number (<= 50 chars)."""
    return "ERP-" + hashlib.sha256(source_key(source).encode()).hexdigest()[:24].upper()
