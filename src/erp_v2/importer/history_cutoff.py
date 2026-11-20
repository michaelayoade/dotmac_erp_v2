"""History cutoff (B-13): closed years reach v2 only through the B-9 opening journal.

``--history-cutoff 2025-12-31`` removes, from the journal, journal-line and posted-ledger-line pairings, every
legacy row dated on or before the cutoff. Without it the B-9 opening (which carries the cumulative legacy trial
balance at the cutoff) would double count every pre-cutoff line the importer also copied.

Rules (never guessed):

* A journal is pre-cutoff when ``COALESCE(posting_date, entry_date) <= cutoff``.
* A journal line has no date of its own: it follows its parent journal. A line whose parent journal does not exist
  is not excluded (it stays a named FK exception, never silently dropped).
* A posted ledger line is pre-cutoff by its OWN ``posting_date`` (the opening trial balance is computed from that
  column, ``migration_ledger.legacy_ledger``).
* An undated journal, journal line (parent undated) or ledger line is a named row refusal
  (``history_cutoff_undated``): v2 ``posting_date`` is NOT NULL, so it cannot land, and it is not provably closed.
* A post-cutoff journal that reverses a pre-cutoff one lands as is with the link empty; the legacy target id is kept
  as evidence (``reversal_target_before_cutoff``) in the census and side file, and is not counted against completeness.
* A journal whose posted ledger lines do not balance (P-5, the sub-kobo journals) is refused as a UNIT (journal, lines,
  ledger lines): counted per table and itemised (key, debit, credit) in the census and side file for the E15 register
  item. It is not a row exception, so the run is not incomplete because of it.
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
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa

from erp_v2.migration_ledger.common import unit_evidence_sha  # one definition; re-exported for B-14

KIND_JOURNAL: Final = "journal"
KIND_JOURNAL_LINE: Final = "journal_line"
KIND_LEDGER_LINE: Final = "ledger_line"
KINDS: Final = frozenset({KIND_JOURNAL, KIND_JOURNAL_LINE, KIND_LEDGER_LINE})

REFUSAL_REQUIRED: Final = "history_cutoff_required"
REFUSAL_INVALID: Final = "history_cutoff_invalid"
REFUSAL_STRADDLE: Final = "history_cutoff_straddling_journals"
REFUSAL_UNDATED: Final = "history_cutoff_undated"
REASON_REVERSAL_TARGET: Final = "reversal_target_before_cutoff"
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
        return ("posting_date", "entry_date", "status")
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


def undated_predicate(kind: str, source_table: str, journal_column: str | None = None) -> str | None:
    """SQL boolean for a row with no date at all: neither provably closed nor importable (v2 ``posting_date`` is NOT
    NULL), so it is a NAMED REFUSAL (``history_cutoff_undated``), never landed. A journal line follows its parent."""

    if kind == KIND_JOURNAL:
        return f"{_journal_pre(source_table + '.')} IS NULL"
    if kind == KIND_LEDGER_LINE:
        return f"{source_table}.posting_date IS NULL"
    if kind == KIND_JOURNAL_LINE and journal_column:
        return (
            f"EXISTS (SELECT 1 FROM {_PARENT_TABLE} hc_u WHERE hc_u.{_PARENT_KEY} = {source_table}.{journal_column} "  # noqa: S608
            f"AND {_journal_pre('hc_u.')} IS NULL)"
        )
    return None


UNBALANCED_UNITS: Final = (
    "(SELECT journal_entry_id FROM posted_ledger_line GROUP BY journal_entry_id "
    "HAVING sum(debit_amount) <> sum(credit_amount))"
)
UNBALANCED_AMOUNTS_SQL: Final = (
    "SELECT journal_entry_id AS k, sum(debit_amount) AS d, sum(credit_amount) AS c FROM posted_ledger_line "
    "GROUP BY journal_entry_id HAVING sum(debit_amount) <> sum(credit_amount)"
)
REASON_UNBALANCED_UNIT: Final = "unbalanced_journal_unit"


def unit_refusal_predicate(kind: str, source_table: str, key_column: str, journal_column: str | None) -> str:
    """SQL boolean, true for a row of a journal whose posted ledger lines do not balance (the 3 sub-kobo journals,
    P-5). The journal, its lines and its ledger lines are refused TOGETHER as a unit (E15): the owner trigger forbids
    posting it and a DRAFT carrying ledger lines can be neither posted nor voided. Not a row exception (F2 must not
    exit incomplete): the register item comes from the census and the side file, and its 2-dp effect and residue go
    through the rounding path."""

    column = key_column if kind == KIND_JOURNAL else journal_column
    if not column:
        raise ValueError("a unit refusal needs the journal column")
    return f"{source_table}.{column} IN {UNBALANCED_UNITS}"


def c_collated(connection: sa.Connection, expression: str) -> str:
    """``expression`` ordered byte-wise on PostgreSQL (``COLLATE "C"``) so the key digest re-derives the same as
    Python ``sorted()``; SQLite's default is already binary."""

    return f'{expression} COLLATE "C"' if connection.dialect.name == "postgresql" else expression


def keys_sha256(keys: Iterable[str]) -> str:
    """sha256 over the keys in the order given (the census reads them ``ORDER BY CAST(key AS TEXT)``)."""

    digest = hashlib.sha256()
    for key in keys:
        digest.update(key.encode())
        digest.update(b"\n")
    return digest.hexdigest()


STRADDLE_IDS_SQL: Final = (  # constant identifiers only; the cutoff is a bound parameter
    "SELECT l.journal_entry_id FROM posted_ledger_line l "  # noqa: S608
    f"LEFT JOIN {_PARENT_TABLE} j ON j.{_PARENT_KEY} = l.journal_entry_id "
    "GROUP BY l.journal_entry_id, j.journal_entry_id, COALESCE(j.posting_date, j.entry_date) "
    "HAVING max(CASE WHEN l.posting_date <= :history_cutoff THEN 1 ELSE 0 END) "
    "<> min(CASE WHEN l.posting_date <= :history_cutoff THEN 1 ELSE 0 END) "
    "OR (j.journal_entry_id IS NOT NULL "
    "AND min(CASE WHEN l.posting_date <= :history_cutoff THEN 1 ELSE 0 END) "
    "<> CASE WHEN COALESCE(j.posting_date, j.entry_date) <= :history_cutoff THEN 1 ELSE 0 END)"
)
STRADDLE_SQL: Final = f"SELECT count(*) FROM ({STRADDLE_IDS_SQL}) s"  # noqa: S608
REASON_STRADDLE: Final = "journal_straddles_cutoff"  # register class E39
CLASS_STRADDLE: Final = "E39"


def straddle_predicate(kind: str, source_table: str, key_column: str, journal_column: str | None) -> str:
    """SQL boolean, true for a row of a straddling journal (STRADDLE_SPEC). The journal, its lines and its ledger
    lines are excluded TOGETHER as a unit: landing it double counts its pre-cutoff lines (already in the opening),
    skipping part of it drops lines, and a partial landing cannot post."""

    column = key_column if kind == KIND_JOURNAL else journal_column
    if not column:
        raise ValueError("a straddle exclusion needs the journal column")
    return f"{source_table}.{column} IN ({STRADDLE_IDS_SQL})"


def not_landed_predicate(kind: str, source_table: str, key_column: str, journal_column: str | None) -> str:
    """Rows the importer does NOT land for any cutoff reason: pre-cutoff, straddling, or unbalanced-unit."""

    return (
        f"({exclusion_predicate(kind, source_table, journal_column)} "
        f"OR {straddle_predicate(kind, source_table, key_column, journal_column)} "
        f"OR {unit_refusal_predicate(kind, source_table, key_column, journal_column)})"
    )


@dataclass(frozen=True)
class Straddler:
    """One legacy journal that crosses the cutoff, classified from its posted ledger lines.

    ``kind`` S1: its ledger lines fall on both sides of the cutoff. S2: all its lines fall on one side, but the journal
    date is on the other. ``pre_net`` is already inside the opening TB (the recompute includes every line);
    ``post_net`` (per day, per account) is what the legacy ledger holds after the cutoff; ``s_j`` = sum of ``pre_net``.
    """

    journal: str
    kind: str
    status: str | None
    journal_date: str | None
    source_module: str | None
    source_document_type: str | None
    source_document_id: str | None
    currency: str | None
    lines: tuple[dict[str, Any], ...]
    pre_net: dict[str, str]
    post_net: dict[str, dict[str, str]]
    s_j: str
    evidence_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "class": CLASS_STRADDLE,
            "reason": REASON_STRADDLE,
            "journal": self.journal,
            "type": self.kind,
            "legacy_status": self.status,
            "journal_date": self.journal_date,
            "source_module": self.source_module,
            "source_document_type": self.source_document_type,
            "source_document_id": self.source_document_id,
            "currency": self.currency,
            "pre_net": self.pre_net,
            "post_net": self.post_net,
            "s_j": self.s_j,
            "evidence_sha256": self.evidence_sha256,
            "lines": list(self.lines),
        }


_CLASSIFY_SQL: Final = (
    "SELECT l.journal_entry_id, l.ledger_line_id, l.journal_line_id, l.account_id, l.account_code, l.posting_date, "  # noqa: S608
    "l.debit_amount, l.credit_amount, l.original_currency_code, j.status, j.posting_date AS jpd, j.entry_date AS jed, "
    "j.source_module, j.source_document_type, j.source_document_id, j.currency_code "
    f"FROM posted_ledger_line l LEFT JOIN {_PARENT_TABLE} j ON j.{_PARENT_KEY} = l.journal_entry_id "
    f"WHERE l.journal_entry_id IN ({STRADDLE_IDS_SQL}) ORDER BY l.journal_entry_id, l.ledger_line_id"
)


def _day(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _iso(value: object) -> str | None:
    day = _day(value)
    return None if day is None else day.isoformat()


def line_payload(r: Any) -> dict[str, Any]:
    """One ledger line as the side files carry it (the keys B-14 requires)."""

    return {
        "ledger_line_id": str(r["ledger_line_id"]),
        "journal_line_id": None if r["journal_line_id"] is None else str(r["journal_line_id"]),
        "account_id": str(r["account_id"]),
        "account_code": r["account_code"],
        "posting_date": _iso(r["posting_date"]),
        "debit": format(Decimal(r["debit_amount"]), "f"),
        "credit": format(Decimal(r["credit_amount"]), "f"),
        "original_currency": r["original_currency_code"],
    }


def classify_straddlers(source: sa.Connection, cutoff: date) -> tuple[Straddler, ...]:
    """Every straddling journal with its type, nets and ledger lines. Raises ``HistoryCutoffRefused`` when the census
    cannot be read (an unreadable source is never an empty one) or when the classifier and the exclusion predicate
    disagree about which journals straddle."""

    try:
        with source.begin_nested():
            rows = source.execute(bind(_CLASSIFY_SQL, cutoff)).mappings().all()
            expected = int(source.execute(bind(STRADDLE_SQL, cutoff)).scalar_one())
    except sa.exc.SQLAlchemyError as exc:
        raise HistoryCutoffRefused(REFUSAL_UNREADABLE) from exc
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row["journal_entry_id"]), []).append(row)
    if len(grouped) != expected:
        raise HistoryCutoffRefused(REFUSAL_STRADDLE, "the classifier and the exclusion disagree")
    out: list[Straddler] = []
    zero = Decimal(0)
    for journal, lines in sorted(grouped.items()):
        first = lines[0]
        jday = _day(first["jpd"]) or _day(first["jed"])
        pre_flags = [(_day(r["posting_date"]) or date.max) <= cutoff for r in lines]
        kind = "S1" if any(pre_flags) and not all(pre_flags) else "S2"
        pre: dict[str, Decimal] = {}
        post: dict[str, dict[str, Decimal]] = {}
        payload = []
        for r, is_pre in zip(lines, pre_flags, strict=True):
            net = Decimal(r["debit_amount"]) - Decimal(r["credit_amount"])
            account = str(r["account_id"])
            if is_pre:
                pre[account] = pre.get(account, zero) + net
            else:
                day = _iso(r["posting_date"]) or ""
                post.setdefault(day, {})
                post[day][account] = post[day].get(account, zero) + net
            payload.append(line_payload(r))
        evidence = unit_evidence_sha(payload)
        out.append(
            Straddler(
                journal=journal,
                kind=kind,
                status=first["status"],
                journal_date=None if jday is None else jday.isoformat(),
                source_module=first["source_module"],
                source_document_type=first["source_document_type"],
                source_document_id=None if first["source_document_id"] is None else str(first["source_document_id"]),
                currency=first["currency_code"],
                lines=tuple(payload),
                pre_net={a: format(v, "f") for a, v in sorted(pre.items())},
                post_net={d: {a: format(v, "f") for a, v in sorted(m.items())} for d, m in sorted(post.items())},
                s_j=format(sum(pre.values(), zero), "f"),
                evidence_sha256=evidence,
            )
        )
    return tuple(out)


CLASS_SUBKOBO: Final = "E15"
REASON_SUBKOBO: Final = REASON_UNBALANCED_UNIT

_UNBALANCED_CLASSIFY_SQL: Final = (
    "SELECT l.journal_entry_id, l.ledger_line_id, l.journal_line_id, l.account_id, l.account_code, l.posting_date, "  # noqa: S608
    "l.debit_amount, l.credit_amount, l.original_currency_code, j.status, j.posting_date AS jpd, j.entry_date AS jed, "
    "j.source_module, j.source_document_type, j.source_document_id, j.currency_code "
    f"FROM posted_ledger_line l LEFT JOIN {_PARENT_TABLE} j ON j.{_PARENT_KEY} = l.journal_entry_id "
    f"WHERE l.journal_entry_id IN {UNBALANCED_UNITS} "
    f"AND l.journal_entry_id NOT IN ({STRADDLE_IDS_SQL}) "
    "AND NOT COALESCE(COALESCE(j.posting_date, j.entry_date) <= :history_cutoff, FALSE) "
    "ORDER BY l.journal_entry_id, l.ledger_line_id"
)


@dataclass(frozen=True)
class UnbalancedUnit:
    """One legacy journal whose posted ledger lines do not balance (P-5, the sub-kobo journals), refused as a unit
    (E15). Same side-file shape as ``Straddler``: ``post_net`` per day and account, ``net`` = debit - credit."""

    journal: str
    status: str | None
    journal_date: str | None
    source_module: str | None
    source_document_type: str | None
    source_document_id: str | None
    currency: str | None
    lines: tuple[dict[str, Any], ...]
    post_net: dict[str, dict[str, str]]
    net: str
    evidence_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "class": CLASS_SUBKOBO,
            "reason": REASON_SUBKOBO,
            "journal": self.journal,
            "type": "UNBALANCED",
            "legacy_status": self.status,
            "journal_date": self.journal_date,
            "source_module": self.source_module,
            "source_document_type": self.source_document_type,
            "source_document_id": self.source_document_id,
            "currency": self.currency,
            "post_net": self.post_net,
            "net": self.net,
            "evidence_sha256": self.evidence_sha256,
            "lines": list(self.lines),
        }


def classify_unbalanced(source: sa.Connection, cutoff: date) -> tuple[UnbalancedUnit, ...]:
    """The unbalanced journals the importer refuses as units (post-cutoff, not straddlers), with full ledger lines."""

    try:
        with source.begin_nested():
            rows = source.execute(bind(_UNBALANCED_CLASSIFY_SQL, cutoff)).mappings().all()
    except sa.exc.SQLAlchemyError as exc:
        raise HistoryCutoffRefused(REFUSAL_UNREADABLE) from exc
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row["journal_entry_id"]), []).append(row)
    out: list[UnbalancedUnit] = []
    zero = Decimal(0)
    for journal, lines in sorted(grouped.items()):
        first = lines[0]
        jday = _day(first["jpd"]) or _day(first["jed"])
        post: dict[str, dict[str, Decimal]] = {}
        net = zero
        payload = []
        for r in lines:
            value = Decimal(r["debit_amount"]) - Decimal(r["credit_amount"])
            net += value
            day = _iso(r["posting_date"]) or ""
            account = str(r["account_id"])
            post.setdefault(day, {})
            post[day][account] = post[day].get(account, zero) + value
            payload.append(line_payload(r))
        out.append(
            UnbalancedUnit(
                journal=journal,
                status=first["status"],
                journal_date=None if jday is None else jday.isoformat(),
                source_module=first["source_module"],
                source_document_type=first["source_document_type"],
                source_document_id=None if first["source_document_id"] is None else str(first["source_document_id"]),
                currency=first["currency_code"],
                lines=tuple(payload),
                post_net={d: {a: format(v, "f") for a, v in sorted(m.items())} for d, m in sorted(post.items())},
                net=format(net, "f"),
                evidence_sha256=unit_evidence_sha(payload),
            )
        )
    return tuple(out)


def write_private_json(path: Any, body: dict[str, Any]) -> str:
    """Mode-600 JSON file (owner only); returns its sha256. Raises OSError."""

    import os

    data = (json.dumps(body, indent=1, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "CLASS_STRADDLE",
    "CLASS_SUBKOBO",
    "KINDS",
    "KIND_JOURNAL",
    "KIND_JOURNAL_LINE",
    "KIND_LEDGER_LINE",
    "MISSING_COLUMN",
    "REASON_STRADDLE",
    "REASON_SUBKOBO",
    "REASON_UNBALANCED_UNIT",
    "REFUSAL_INVALID",
    "REFUSAL_REQUIRED",
    "REFUSAL_STRADDLE",
    "STRADDLE_SQL",
    "UNBALANCED_AMOUNTS_SQL",
    "HistoryCutoffRefused",
    "Straddler",
    "UnbalancedUnit",
    "bind",
    "c_collated",
    "classify_straddlers",
    "classify_unbalanced",
    "exclusion_predicate",
    "keys_sha256",
    "line_payload",
    "not_landed_predicate",
    "parse_cutoff",
    "required_source_columns",
    "straddle_predicate",
    "undated_predicate",
    "unit_evidence_sha",
    "unit_refusal_predicate",
    "write_private_json",
]
