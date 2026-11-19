"""History cutoff (B-13): closed years reach v2 only through the B-9 opening journal.

``--history-cutoff 2025-12-31`` removes, from the journal, journal-line and posted-ledger-line pairings, every
legacy row dated on or before the cutoff. Without it the B-9 opening (which carries the cumulative legacy trial
balance at the cutoff) would double count every pre-cutoff line the importer also copied.

Rules (never guessed):

* A journal is pre-cutoff when ``COALESCE(posting_date, entry_date) <= cutoff``. A journal with NO date is not
  provably pre-cutoff, so it is not excluded; it is counted as ``undated`` in the receipt census.
* A journal line has no date of its own: it follows its parent journal. A line whose parent journal does not exist
  is not excluded (it stays a named FK exception, never silently dropped).
* A posted ledger line is pre-cutoff by its OWN ``posting_date`` (the opening trial balance is computed from that
  column, ``migration_ledger.legacy_ledger``).
* A journal whose ledger lines straddle the cutoff, or whose lines disagree with its own date, cannot land half of
  itself: the run refuses (``history_cutoff_straddling_journals``) before any write.
* Documents dated before the cutoff keep their legacy journal id as evidence: no destination document table has a
  foreign key to the journal tables (pinned by a test), so excluding a journal never excludes or rewrites a
  document, and the excluded journal ids are recorded in the receipt census as a count and a sha256 over the
  sorted ids.

The cutoff is required whenever a pairing declares one: the executor raises without it (fail closed), and the CLI
refuses before touching either database.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date
from typing import Final

import sqlalchemy as sa

KIND_JOURNAL: Final = "journal"
KIND_JOURNAL_LINE: Final = "journal_line"
KIND_LEDGER_LINE: Final = "ledger_line"
KINDS: Final = frozenset({KIND_JOURNAL, KIND_JOURNAL_LINE, KIND_LEDGER_LINE})

REFUSAL_REQUIRED: Final = "history_cutoff_required"
REFUSAL_INVALID: Final = "history_cutoff_invalid"
REFUSAL_STRADDLE: Final = "history_cutoff_straddling_journals"
REFUSAL_UNREADABLE: Final = "history_cutoff_census_unreadable"
MISSING_COLUMN: Final = "history_cutoff_date_column"  # recorded in spec.missing_required: a spec that cannot filter
# is not executable

PARAM: Final = "history_cutoff"

# Legacy names (gl.journal_entry / gl.posted_ledger_line), resolved through the connection search_path like the
# enrolment SQL does.
_PARENT_TABLE: Final = "journal_entry"
_PARENT_KEY: Final = "journal_entry_id"
_JOURNAL_DATE: Final = "COALESCE({a}posting_date, {a}entry_date)"


class HistoryCutoffRefused(ValueError):
    """The cutoff is missing, unusable or contradicted by the source; carries a stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def parse_cutoff(value: str | date | None) -> date:
    """The cutoff as a calendar date; anything else is a named refusal (never a datetime, never a guess)."""

    if value is None or (isinstance(value, str) and not value.strip()):
        raise HistoryCutoffRefused(REFUSAL_REQUIRED)
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise HistoryCutoffRefused(REFUSAL_INVALID, "expected an ISO date such as 2025-12-31") from exc


def required_source_columns(kind: str, journal_column: str | None) -> tuple[str, ...]:
    """Source columns the filter reads on the spec's own table (empty: nothing to verify)."""

    if kind == KIND_JOURNAL:
        return ("posting_date", "entry_date")
    if kind == KIND_LEDGER_LINE:
        return ("posting_date",)
    if kind == KIND_JOURNAL_LINE and journal_column:
        return (journal_column,)
    return ()


def _journal_pre(alias: str = "") -> str:
    return _JOURNAL_DATE.format(a=alias)


def exclusion_predicate(kind: str, source_table: str, journal_column: str | None) -> str:
    """SQL boolean, true for a row that is provably on or before the cutoff (bound parameter ``:history_cutoff``).

    Identifiers are validated by the caller (spec.py); the cutoff is only ever a bound parameter.
    """

    if kind == KIND_JOURNAL:
        return f"COALESCE({_journal_pre(source_table + '.')} <= :history_cutoff, FALSE)"
    if kind == KIND_LEDGER_LINE:
        return f"COALESCE({source_table}.posting_date <= :history_cutoff, FALSE)"
    if kind == KIND_JOURNAL_LINE:
        if not journal_column:
            raise ValueError("a journal-line cutoff needs the source journal column")
        return (
            f"EXISTS (SELECT 1 FROM {_PARENT_TABLE} hc_j WHERE hc_j.{_PARENT_KEY} = {source_table}.{journal_column} "  # noqa: S608
            f"AND COALESCE({_journal_pre('hc_j.')} <= :history_cutoff, FALSE))"
        )
    raise ValueError(f"unknown history cutoff kind {kind!r}")


def bind(statement: str, cutoff: date) -> sa.TextClause:
    return sa.text(statement).bindparams(sa.bindparam(PARAM, cutoff, type_=sa.Date))


def undated_predicate(kind: str, source_table: str) -> str | None:
    """SQL boolean for a row with no date at all (not provably pre-cutoff, so it lands and is counted)."""

    if kind == KIND_JOURNAL:
        return f"{_journal_pre(source_table + '.')} IS NULL"
    if kind == KIND_LEDGER_LINE:
        return f"{source_table}.posting_date IS NULL"
    return None


def keys_sha256(keys: Iterable[str]) -> str:
    """sha256 over the keys in the order given (the census reads them ``ORDER BY CAST(key AS TEXT)``)."""

    digest = hashlib.sha256()
    for key in keys:
        digest.update(key.encode())
        digest.update(b"\n")
    return digest.hexdigest()


STRADDLE_SQL: Final = (  # constant identifiers only; the cutoff is a bound parameter
    "SELECT count(*) FROM ("  # noqa: S608
    "SELECT l.journal_entry_id FROM posted_ledger_line l "
    f"LEFT JOIN {_PARENT_TABLE} j ON j.{_PARENT_KEY} = l.journal_entry_id "
    "GROUP BY l.journal_entry_id, j.journal_entry_id, COALESCE(j.posting_date, j.entry_date) "
    "HAVING bool_or(l.posting_date <= :history_cutoff) <> bool_and(l.posting_date <= :history_cutoff) "
    "OR (j.journal_entry_id IS NOT NULL AND bool_and(COALESCE(l.posting_date <= :history_cutoff, FALSE)) "
    "<> COALESCE(COALESCE(j.posting_date, j.entry_date) <= :history_cutoff, FALSE))"
    ") s"
)


def straddling_journals(source: sa.Connection, cutoff: date) -> int:
    """Journals whose ledger lines are not all on one side of the cutoff, or disagree with the journal date.

    Raises ``HistoryCutoffRefused`` when the census cannot be read: an unreadable source is never an empty one.
    """

    try:
        with source.begin_nested():
            return int(source.execute(bind(STRADDLE_SQL, cutoff)).scalar_one())
    except sa.exc.SQLAlchemyError as exc:
        raise HistoryCutoffRefused(REFUSAL_UNREADABLE) from exc


__all__ = [
    "KINDS",
    "KIND_JOURNAL",
    "KIND_JOURNAL_LINE",
    "KIND_LEDGER_LINE",
    "MISSING_COLUMN",
    "REFUSAL_INVALID",
    "REFUSAL_REQUIRED",
    "REFUSAL_STRADDLE",
    "HistoryCutoffRefused",
    "bind",
    "exclusion_predicate",
    "keys_sha256",
    "parse_cutoff",
    "required_source_columns",
    "straddling_journals",
    "undated_predicate",
]
