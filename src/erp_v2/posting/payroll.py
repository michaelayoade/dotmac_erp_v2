"""Payroll consequence -> bridge input.

Starter payroll a3 consequences mirror the payables shape but carry no
``exchange_rate`` (payroll is functional-currency only). This thin function
refuses unless the consequence currency equals the tenant's functional currency
(supplied by the caller from its own tenant configuration, never guessed here),
supplies exchange rate 1, and posts through the bridge. Nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from dotmac_kernel.cache import TenantScope
from sqlalchemy.orm import Session

from erp_v2.posting.bridge import PostingResult, post_consequence
from erp_v2.posting.rules import PostingRefused

_REQUIRED = ("source", "posting_date", "description", "currency_code", "minor_units", "entries", "fingerprint")


@dataclass(frozen=True, slots=True)
class _Functional:
    source: object
    posting_date: date
    description: str
    currency_code: str
    exchange_rate: Decimal
    entries: tuple
    fingerprint: str


def to_bridge_input(consequence: object, *, functional_currency: str) -> _Functional:
    if any(not hasattr(consequence, name) for name in _REQUIRED):
        raise PostingRefused("invalid_consequence", "not a payroll accounting consequence")
    if not isinstance(functional_currency, str) or len(functional_currency) != 3:
        raise PostingRefused("functional_currency_required")
    currency = consequence.currency_code  # type: ignore[attr-defined]
    if not isinstance(currency, str) or currency != functional_currency.upper():
        raise PostingRefused("non_functional_currency", "payroll posts in the tenant functional currency only")
    return _Functional(
        source=consequence.source,  # type: ignore[attr-defined]
        posting_date=consequence.posting_date,  # type: ignore[attr-defined]
        description=consequence.description,  # type: ignore[attr-defined]
        currency_code=currency,
        exchange_rate=Decimal(1),
        entries=tuple(consequence.entries),  # type: ignore[attr-defined]
        fingerprint=consequence.fingerprint,  # type: ignore[attr-defined]
    )


def post_payroll_consequence(
    db: Session,
    *,
    scope: TenantScope,
    consequence: object,
    functional_currency: str,
    approval_ref: str,
    actor: str,
) -> PostingResult:
    return post_consequence(
        db,
        scope=scope,
        consequence=to_bridge_input(consequence, functional_currency=functional_currency),
        approval_ref=approval_ref,
        actor=actor,
    )
