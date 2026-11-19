"""Verifier evidence for a legacy journal the history cutoff (B-13) did not import.

An AP item whose legacy journal is dated on or before the cutoff is still admissible: that journal is in the opening
trial balance, NOT in v2. The verifiers therefore substitute the evidence for it (they do not drop a check):

* PROVEN on the snapshot with the same predicate the importer used (``COALESCE(posting_date, entry_date) <= cutoff``)
  and the receipt's cutoff (bound in the transform manifest binding): the journal is dated on or before it AND it has
  posted ledger lines, every one dated on or before it (so it is in the opening TB);
* ASSERTED ABSENT in v2: no journal, journal line or posted ledger line for it exists in the destination.

A journal dated after the cutoff takes the ordinary path (and a journal whose ledger lines are on the other side of
the cutoff than the journal itself is refused). An undated journal is refused: it is neither provably closed nor
importable (``journal_undated``).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def journal_proof(
    journal: dict[str, Any], ledger: list[dict[str, Any]], cutoff: date | None, require: Any
) -> dict[str, Any] | None:
    """The cutoff evidence for this journal, or None when it is not cutoff-excluded (ordinary path).

    ``require(condition, reason)`` raises the caller's evidence error."""

    if cutoff is None:
        return None
    day = _as_date(journal.get("posting_date")) or _as_date(journal.get("entry_date"))
    require(day is not None, "journal_undated")
    ledger_days = [_as_date(r.get("posting_date")) for r in ledger]
    require(all(d is not None for d in ledger_days), "journal_ledger_line_undated")
    if day > cutoff:
        require(all(d > cutoff for d in ledger_days), "journal_straddles_history_cutoff")
        return None
    require(bool(ledger) and all(d <= cutoff for d in ledger_days), "journal_straddles_history_cutoff")
    debit = sum((Decimal(r["debit_amount"]) for r in ledger), Decimal(0))
    credit = sum((Decimal(r["credit_amount"]) for r in ledger), Decimal(0))
    return {
        "history_cutoff": cutoff.isoformat(),
        "journal_date": day.isoformat(),
        "ledger_lines": len(ledger),
        "ledger_debit": format(debit, "f"),
        "ledger_credit": format(credit, "f"),
        "destination": "absent",
    }


__all__ = ["journal_proof"]
