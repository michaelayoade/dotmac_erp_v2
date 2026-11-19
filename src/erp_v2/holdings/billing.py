"""The billing side of the 1400 and prepayment ties (R1 and per-account positions), behind a protocol.

Billing a12 has no R1 (``get_receivable_control_position_v1`` arrives with a13), so the default reader refuses
``r1_unavailable``, exactly as B-9r does. Tests and the a13 binding supply a ``BillingReader``. Amounts are MINOR
units (integers); ``position_fingerprint`` binds a plan to the state it read.

R1 per currency: ``collectible_minor`` (open receivables, unknown_unverified counted: P-2), ``non_cash_minor``,
``available_credit_minor``, ``prepaid_funding_minor``. 1400 subledger = collectible - non_cash; prepayment subledger =
available_credit + prepaid_funding (both signed into GL terms by the caller: 1400 debit +, prepayment credit -).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from erp_v2.holdings.errors import refuse


@dataclass(frozen=True, slots=True)
class Position:
    collectible_minor: int
    non_cash_minor: int
    available_credit_minor: int
    prepaid_funding_minor: int

    def receivable(self) -> int:
        return self.collectible_minor - self.non_cash_minor

    def credit(self) -> int:
        return self.available_credit_minor + self.prepaid_funding_minor


@dataclass(frozen=True, slots=True)
class R1Snapshot:
    position_fingerprint: str
    currencies: Mapping[str, Position]


class BillingReader(Protocol):
    def r1(self, db: Session) -> R1Snapshot:
        """Billing R1, read in a REPEATABLE READ READ ONLY transaction."""

    def account_positions(self, db: Session) -> Mapping[tuple[str, str], Position]:
        """Per (legacy customer key, currency); the caller opens a READ COMMITTED read-write transaction it rolls
        back (``get_current_receivable_position`` needs read-write [E R1.1])."""


class UnavailableBilling:
    """Billing a12: no R1. Every read refuses ``r1_unavailable``."""

    def r1(self, db: Session) -> R1Snapshot:
        return refuse("r1_unavailable")

    def account_positions(self, db: Session) -> Mapping[tuple[str, str], Position]:
        return refuse("r1_unavailable")


def default_reader() -> BillingReader:
    try:
        from dotmac_billing import queries
    except ImportError:  # pragma: no cover - billing is a hard dependency
        return UnavailableBilling()
    if getattr(queries, "get_receivable_control_position_v1", None) is None:
        return UnavailableBilling()
    refuse("billing_reader_not_bound", "bind the a13 reader (no adapter is built on a12)")
    raise AssertionError  # pragma: no cover
