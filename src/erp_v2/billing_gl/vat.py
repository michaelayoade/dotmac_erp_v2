"""Cash-basis output-VAT arithmetic for the billing -> GL mapper. Pure: no database, no billing import.

Ruling (2026-11-18): output VAT is deferred on 1455 at issue and released to 1440 on payment AND on application of an
advance. A VAT position is one (document, treatment) with D = the tax deferred at issue and T = the document total.

Vocabulary, per position k of a document:
  R_k   remaining deferral = the 1455 balance attributable to the position (Σ ``deferral_delta``);
  open  the document's open amount = open_initial + Σ ``open_delta`` (recorded once per fact, on the carrier row).
A movement moves R_k by ``deferral_delta`` and the 1440 side by ``current_delta``; the journal lines are derived from
the movements (Cr is positive, Dr negative), never typed.

Rules (VAT-spec 4.3/4.4):
  release (settled delta d > 0)   r_k = min(R_k, HALF_EVEN(d x D_k / T)); the settlement that brings open to 0 takes R_k
  unrelease (deallocation x)      full: exactly that allocation's net release; partial: the same formula capped at it
  credit (tax t_k)                Dr deferral min(t_k, R_k), Dr current the rest; then, if open reaches 0, close out R_k
  void / reversal                 the exact negation of the predecessor's movements
Invariants after every fact: 0 <= R_k <= D_k, 0 <= open <= T, and open = 0 implies every R_k = 0. A violation is
refused (``vat_invariant``); nothing is clamped silently.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final
from uuid import UUID

ZERO: Final = Decimal(0)
CLOSEOUT_PREFIX: Final = "closeout:"


class VatRefused(ValueError):
    """A stable, machine-readable block reason; nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def quantize(amount: Decimal, minor_units: int = 2) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-minor_units), rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class Move:
    treatment: str
    kind: str
    deferral_delta: Decimal
    current_delta: Decimal
    open_delta: Decimal = ZERO
    carrier: bool = False
    allocation_ref: str = ""


@dataclass(slots=True)
class Position:
    id: UUID | None
    treatment: str
    deferral_account: str | None
    current_account: str | None
    deferred: Decimal  # D_k
    remaining: Decimal  # R_k


@dataclass(slots=True)
class DocVat:
    document_id: UUID
    total: Decimal  # T
    open: Decimal
    positions: dict[str, Position]
    # The denominator of the release rate: the gross the deferral still covers. For a native invoice it is T; for a
    # migrated, part-paid invoice it is the remaining gross at the opening (open_initial), because D is the remaining
    # deferral (spec amendment to 4.3: rate = remaining deferral / remaining gross, not D / original total).
    basis: Decimal = ZERO
    # allocation ref -> {treatment: net release}, and settled amount per allocation ref.
    net_release: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(dict))
    settled: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    # close-out tag -> {treatment: net amount moved by the close-out}
    closeouts: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(dict))

    def order(self) -> list[Position]:
        return [self.positions[k] for k in sorted(self.positions)]

    def carrier(self) -> str:
        return sorted(self.positions)[0]


def apply(doc: DocVat, moves: tuple[Move, ...]) -> None:
    """Fold moves into the in-memory state (so a later row of the same fact sees them) and enforce the invariants."""
    for move in moves:
        pos = doc.positions[move.treatment]
        pos.remaining += move.deferral_delta
        if move.carrier:
            doc.open += move.open_delta
        ref = move.allocation_ref
        if ref and move.kind in ("release", "unrelease", "reverse"):
            doc.net_release[ref][move.treatment] = doc.net_release[ref].get(move.treatment, ZERO) - move.deferral_delta
            if move.carrier:
                doc.settled[ref] -= move.open_delta
        if ref.startswith(CLOSEOUT_PREFIX):
            doc.closeouts[ref][move.treatment] = doc.closeouts[ref].get(move.treatment, ZERO) - move.deferral_delta
    check(doc)


def check(doc: DocVat) -> None:
    if doc.open < 0 or doc.open > doc.basis:
        raise VatRefused("vat_invariant")
    for pos in doc.positions.values():
        if pos.remaining < 0 or pos.remaining > pos.deferred:
            raise VatRefused("vat_invariant")
    if doc.open == 0 and any(pos.remaining != 0 for pos in doc.positions.values()):
        raise VatRefused("vat_invariant")


def _first(doc: DocVat, treatment: str) -> bool:
    return treatment == doc.carrier()


def release(doc: DocVat, delta: Decimal, ref: str, minor_units: int = 2) -> tuple[Move, ...]:
    """A settlement or advance application of ``delta`` (> 0) against the document."""
    if delta <= 0:
        raise VatRefused("vat_invariant")
    if doc.open - delta < 0:
        raise VatRefused("vat_invariant")
    closes = doc.open - delta == 0
    moves = []
    for pos in doc.order():
        share = pos.remaining if closes else min(pos.remaining, quantize(delta * pos.deferred / doc.basis, minor_units))
        carrier = _first(doc, pos.treatment)
        moves.append(Move(pos.treatment, "release", -share, share, -delta if carrier else ZERO, carrier, ref))
    return tuple(moves)


def unrelease(doc: DocVat, delta: Decimal, offsets_ref: str, minor_units: int = 2) -> tuple[Move, ...]:
    """A deallocation of ``delta`` (> 0, the magnitude) against the allocation ``offsets_ref``."""
    if offsets_ref not in doc.net_release and offsets_ref not in doc.settled:
        raise VatRefused("vat_allocation_unresolved")
    if delta <= 0 or delta > doc.settled[offsets_ref] or doc.open + delta > doc.basis:
        raise VatRefused("vat_invariant")
    moves: list[Move] = []
    # A close-out that made open = 0 is reversed first (exact), because the document is open again.
    if doc.open == 0:
        for tag, amounts in sorted(doc.closeouts.items()):
            for treatment in sorted(amounts):
                amount = amounts[treatment]
                if amount != 0:
                    moves.append(Move(treatment, "uncloseout", amount, -amount, ZERO, False, tag))
    full = delta == doc.settled[offsets_ref]
    for pos in doc.order():
        net = doc.net_release[offsets_ref].get(pos.treatment, ZERO)
        share = net if full else min(net, quantize(delta * pos.deferred / doc.basis, minor_units))
        carrier = _first(doc, pos.treatment)
        moves.append(Move(pos.treatment, "unrelease", share, -share, delta if carrier else ZERO, carrier, offsets_ref))
    return tuple(moves)


def credit(doc: DocVat, taxes: dict[str, Decimal], total: Decimal, tag: str, minor_units: int = 2) -> tuple[Move, ...]:
    """A credit note of ``total`` whose tax is ``taxes`` per treatment, against this (its original) document."""
    for treatment, tax in taxes.items():
        if tax != 0 and treatment not in doc.positions:
            raise VatRefused("vat_position_missing")
    if total <= 0 or doc.open - total < 0:
        raise VatRefused("vat_invariant")
    moves: list[Move] = []
    carrier = doc.carrier()
    remaining = {k: p.remaining for k, p in doc.positions.items()}
    for treatment in sorted(doc.positions):
        tax = taxes.get(treatment, ZERO)
        reduce = min(tax, remaining[treatment])
        current = tax - reduce
        remaining[treatment] -= reduce
        is_carrier = treatment == carrier
        moves.append(Move(treatment, "credit_reduce", -reduce, ZERO, -total if is_carrier else ZERO, is_carrier, ""))
        moves.append(Move(treatment, "credit_current", ZERO, -current, ZERO, False, ""))
    if doc.open - total == 0:
        for treatment in sorted(doc.positions):
            left = remaining[treatment]
            if left > 0:
                moves.append(Move(treatment, "closeout", -left, left, ZERO, False, tag))
    return tuple(moves)


def negate(moves: tuple[Move, ...]) -> tuple[Move, ...]:
    """The mirror of a predecessor's stored movements (a void, or the reversal of an allocation)."""
    return tuple(
        replace(
            m,
            kind="reverse",
            allocation_ref=m.allocation_ref or f"~{m.kind}",  # keeps (position, kind, ref) unique per mirrored row
            deferral_delta=-m.deferral_delta,
            current_delta=-m.current_delta,
            open_delta=-m.open_delta,
        )
        for m in moves
    )


def lines(moves: tuple[Move, ...], doc: DocVat) -> list[tuple[str, Decimal]]:
    """Net signed amount per account (Cr positive, Dr negative), zero nets dropped, in account order."""
    net: dict[str, Decimal] = defaultdict(Decimal)
    for move in moves:
        pos = doc.positions[move.treatment]
        if move.deferral_delta:
            if pos.deferral_account is None:
                raise VatRefused("tax_account_unmapped")
            net[pos.deferral_account] += move.deferral_delta
        if move.current_delta:
            if pos.current_account is None:
                raise VatRefused("tax_account_unmapped")
            net[pos.current_account] += move.current_delta
    return [(account, amount) for account, amount in sorted(net.items()) if amount != 0]


def issue(
    document_id: UUID, total: Decimal, taxes: dict[str, tuple[Decimal, str, str]]
) -> tuple[DocVat, tuple[Move, ...]]:
    """An invoice issue: one position per treatment with tax, D_k deferred (Cr deferral), open = T.

    ``taxes`` maps treatment -> (tax, deferral account, current account). A zero-tax document has no position.
    """
    positions = {
        treatment: Position(None, treatment, deferral, current, tax, ZERO)
        for treatment, (tax, deferral, current) in taxes.items()
        if tax != 0
    }
    doc = DocVat(document_id, total, total, positions, total)
    moves = tuple(Move(t, "defer", p.deferred, ZERO) for t, p in sorted(positions.items()))
    apply(doc, moves)
    return doc, moves
