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
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

QUANTUM: Final = Decimal("0.000001")
# Kernel idempotency MAX_KEY_LENGTH (a107) is 200; a test asserts we never exceed it.
MAX_KEY_LENGTH: Final = 200
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


# One declared form per owner field; each new kind is one reviewed entry plus a
# test in the PR that adds it. Read-only mapping.
# The payroll kinds take the Starter payroll a3 consequence via
# erp_v2.posting.payroll, which refuses non-functional currencies and supplies
# exchange_rate 1 (payroll carries none). Net payable account ref is the account
# CODE "2040" (minting.py); component refs are Account UUIDs.
POLICIES: Final = MappingProxyType(
    {
        ("payables", "supplier_invoice"): RefPolicy(),
        ("payables", "credit_note"): RefPolicy(),
        # payables a6: the deferred input VAT a credit application releases (Dr release / Cr deferral account,
        # Account UUIDs from the owner's declared policy; no 2000 line).
        ("payables", "supplier_credit_application"): RefPolicy(),
        # B3: Dr invoice liability account(s) (Account UUID), Cr the paying bank account's cash GL
        # (Account UUID from the bank account record); a5 WHT accounts are UUIDs too (WHT itself
        # is refused by the ERP workflow, so none are posted).
        ("payables", "supplier_payment"): RefPolicy(),
        ("payables", "supplier_payment_reversal"): RefPolicy(),
        # B-15 live billing -> GL mapper: one journal per billing accounting fact (document_id = fact id, version =
        # fact version, fingerprint = fact digest). Lines are system-built from the fact's lane effects: Dr/Cr the
        # 1400 receivable, the prepayment liability (or the prepaid-funding account) and the settlement source's
        # cash account, all Account UUIDs resolved from the tenant, never typed. W4/B15.6.
        ("billing", "accounting_fact"): RefPolicy(),
        ("payroll", "payroll_run"): RefPolicy(frozenset({"2040"})),
        ("payroll", "payroll_liability_settlement"): RefPolicy(frozenset({"2040"})),
        # payroll a5: bank-returned salary. Dr the ORIGINAL bank cash GL (Account UUID) / Cr 2040.
        ("payroll", "payroll_settlement_reversal"): RefPolicy(frozenset({"2040"})),
        # Staff loan entries (PR-3; no Starter owner): Dr/Cr the 1410 Staff Loan account against the paying
        # bank account's cash GL (advance) or a Finance-chosen non-control offset (adjustment, control
        # difference). Both refs are Account UUIDs, resolved from the tenant's records, never typed.
        ("erp", "staff_loan_entry"): RefPolicy(),
        # B-14 migration holdings (W1: four new system doors onto control accounts). Lines are system-built from
        # re-derived owner state, once-only uuid5 identities, Account UUIDs resolved from the tenant. ONLY
        # erp_v2.holdings builds these kinds (a test greps).
        ("erp.migration", "migration_holding"): RefPolicy(),
        ("erp.migration", "holding_release"): RefPolicy(),
        ("erp.migration", "holding_writeoff"): RefPolicy(),
        ("erp.migration", "ar_opening_credit"): RefPolicy(),
        # B-14 straddle reproduction (W8): a legacy journal dated across the history cutoff, re-posted balanced at its
        # legacy accounts and days; refuses control accounts, so no control door is used.
        ("erp.migration", "migration_straddle"): RefPolicy(),
        # B-14 E15 sub-kobo units: legacy lines re-posted exactly (6 dp) plus one residue line on the rounding account.
        ("erp.migration", "migration_subkobo"): RefPolicy(),
        # S8 claims opening: ONE journal per flip window, system-built from the owner receipts just written (Dr each
        # carried claim-family account 2040/2030/2110, Cr 2045 per claimant), once-only uuid5 identity. A system door
        # onto control accounts 2040 and 2045 (approved plan section 0.2). ONLY erp_v2.claims_opening.reclass builds it.
        ("erp.migration", "claim_family_reclass"): RefPolicy(),
    }
)


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
    """Shape + balance checks; returns the owner's ref policy. No database.

    An object that is not shaped like an AccountingConsequence refuses
    (``invalid_consequence``) rather than raising AttributeError.
    """
    try:
        return _validate(consequence)
    except (AttributeError, TypeError, KeyError) as exc:
        raise PostingRefused("invalid_consequence", "not an accounting consequence") from exc


def _validate(consequence: ConsequenceLike) -> RefPolicy:
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
