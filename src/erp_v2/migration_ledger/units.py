"""Refused units in the bridge: E39 straddlers (T-STRADDLE) and E15 sub-kobo journals (T-SUBKOBO).

The B-13 importer refuses a legacy journal as a UNIT (journal, lines, ledger lines) when it straddles the history
cutoff D (E39, STRADDLE-SPEC W8) or does not balance (E15, the sub-kobo journals; E15-AND-SIDEFILE-RULING). B-14
reproduces each exactly (kinds ``migration_straddle`` / ``migration_subkobo``; E15 adds ONE residue line on the
rounding account). Neither kind is native in ``ledger_check.v2_posted``. The bridge then needs, per unit, items that
cover its legacy cells; this module derives them ONLY from the receipt-bound B-13 side file, never from operator input:

* every line of the unit becomes a line item per (account, period), legacy = v2 = the net (the account is mapped from
  the line's ``account_id``, the file's ``account_code`` is ignored);
* E15 only: one RESIDUE item on (rounding account, period): legacy 0, v2 = ``-net`` (the residue line). It is the only
  non-zero delta, and the residues must sum to the rounding-cell difference.

A ref may sit in one bridge bucket only, so the bridge inputs carry NO other item for these refs.

The side file is #74's real shape (``<receipt>.straddlers.json`` / ``<receipt>.subkobo.json``): top level
``{history_cutoff, class, reason, journals}``; ``post_net`` is NESTED ``{posting_date: {account_id: net}}`` and is
rebuilt from the lines and compared; ``evidence_sha256 = common.unit_evidence_sha(lines)``. The file's sha must equal
the receipt's ``history_cutoff_census.<section>.side_file_sha256``; the receipt's own sha is pinned by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from erp_v2.migration_ledger.common import (
    ZERO,
    day,
    fmt,
    keys_exactly,
    money,
    refuse,
    sha_text,
    text,
    unit_evidence_sha,
)

if TYPE_CHECKING:
    from erp_v2.migration_ledger.bridge import Item

SUBKOBO_RULE: Final = "T-SUBKOBO"
STRADDLE_RULE: Final = "T-STRADDLE"
E15: Final = "E15"
E39: Final = "E39"
REASON_E15: Final = "unbalanced_journal_unit"
REASON_E39: Final = "journal_straddles_cutoff"
RESIDUE: Final = "residue"
LIMIT: Final = Decimal("0.01")
_COMMON = {
    "class", "reason", "journal", "type", "legacy_status", "journal_date", "source_module", "source_document_type",
    "source_document_id", "currency", "lines", "evidence_sha256", "post_net",
}  # fmt: skip
_KEYS = {E15: _COMMON | {"net"}, E39: _COMMON | {"pre_net", "s_j"}}
_REASON = {E15: REASON_E15, E39: REASON_E39}
_SECTION = {E15: "subkobo", E39: "straddlers"}
_RULE = {E15: SUBKOBO_RULE, E39: STRADDLE_RULE}
_TOP = {"history_cutoff", "class", "reason", "journals"}
_LINE = {
    "ledger_line_id", "journal_line_id", "account_id", "account_code", "posting_date", "debit", "credit",
    "original_currency",
}  # fmt: skip


@dataclass(frozen=True)
class Unit:
    cls: str
    journal_id: str
    kind: str  # E15: UNBALANCED; E39: S1 | S2
    day: date | None  # the single ledger day (E15 only)
    net: Decimal  # E15: sum(debit) - sum(credit); the residue line posts -net
    evidence_sha256: str
    cells: tuple[tuple[str, str, Decimal], ...]  # (account code, YYYY-MM, net) of the ledger lines

    @property
    def period(self) -> str:
        return self.day.strftime("%Y-%m")  # type: ignore[union-attr]

    @property
    def residue(self) -> Decimal:
        return -self.net


def _nested(raw: object, code: str) -> dict[str, dict[str, Decimal]]:
    if not isinstance(raw, dict):
        refuse(code)
    out: dict[str, dict[str, Decimal]] = {}
    for d, accounts in raw.items():
        if not isinstance(d, str) or not isinstance(accounts, dict):
            refuse(code)
        out[d] = {text(a, code, 64): money(v, code) for a, v in accounts.items()}
    return out


def parse_side_file(raw: object, cls: str, codes: dict[str, str]) -> tuple[date, tuple[Unit, ...]]:
    """Parse and RE-DERIVE a B-13 side file. Everything the file claims (nets, ``post_net``, evidence) is recomputed
    from its ledger lines; a difference refuses. ``codes`` maps legacy account id -> destination account code."""
    body = keys_exactly(raw, _TOP, "invalid_unit_side_file")
    if body["class"] != cls or body["reason"] != _REASON[cls] or not isinstance(body["journals"], list):
        refuse("invalid_unit_side_file")
    cutoff = day(body["history_cutoff"], "invalid_unit_side_file")
    out: list[Unit] = []
    seen: set[str] = set()
    for entry in body["journals"]:
        j = keys_exactly(entry, _KEYS[cls], "invalid_unit_journal")
        if j["class"] != cls or j["reason"] != _REASON[cls]:
            refuse("invalid_unit_journal")
        journal = text(j["journal"], "invalid_unit_journal", 255)
        if journal in seen:
            refuse("unit_duplicate_journal")
        seen.add(journal)
        lines = j["lines"]
        if not isinstance(lines, list) or not lines:
            refuse("invalid_unit_journal")
        if unit_evidence_sha(lines) != sha_text(j["evidence_sha256"], "invalid_unit_journal"):
            refuse("evidence_sha_mismatch")
        post: dict[str, dict[str, Decimal]] = {}
        pre: dict[str, Decimal] = {}
        cells: dict[tuple[str, str], Decimal] = {}
        days: set[date] = set()
        net = ZERO
        for ln in lines:
            e = keys_exactly(ln, _LINE, "invalid_unit_line")
            account = text(e["account_id"], "invalid_unit_line", 64)
            if account not in codes:
                refuse("legacy_account_unmapped", "a refused unit uses an account that is not in the destination chart")
            when = day(e["posting_date"], "invalid_unit_line")
            amount = money(e["debit"], "invalid_unit_line") - money(e["credit"], "invalid_unit_line")
            net += amount
            if when <= cutoff:
                pre[account] = pre.get(account, ZERO) + amount
            else:
                day_map = post.setdefault(when.isoformat(), {})
                day_map[account] = day_map.get(account, ZERO) + amount
                key = (codes[account], when.strftime("%Y-%m"))
                cells[key] = cells.get(key, ZERO) + amount
                days.add(when)
        if post != _nested(j["post_net"], "invalid_unit_journal"):
            refuse("unit_post_net_differs_from_lines", "post_net is {day: {account_id: net}} rebuilt from the lines")
        if cls == E15:
            if j["type"] != "UNBALANCED":
                refuse("invalid_unit_journal")
            if pre:
                refuse("subkobo_splits_cutoff")
            if len(days) != 1:
                refuse("subkobo_multi_day")
            if money(j["net"], "invalid_unit_journal") != net:
                refuse("subkobo_net_differs_from_lines")
            if not ZERO < abs(net) < LIMIT:
                refuse("subkobo_residue_out_of_range")
            out.append(Unit(cls, journal, "UNBALANCED", next(iter(days)), net, j["evidence_sha256"], _cells(cells)))
        else:
            s_j = sum(pre.values(), ZERO)
            if (
                j["type"] not in ("S1", "S2")
                or _nested({"-": j["pre_net"]}, "invalid_unit_journal")["-"] != pre
                or money(j["s_j"], "invalid_unit_journal") != s_j
            ):
                refuse("unit_pre_net_differs_from_lines")
            if pre or s_j != 0 or j["type"] != "S2":
                refuse(
                    "straddle_not_simple", "only all-post-cutoff journals (type S2, s_j 0) are bridged without suspense"
                )
            if any(sum(m.values(), ZERO) != 0 for m in post.values()):
                refuse("straddle_day_not_balanced")
            out.append(Unit(cls, journal, "S2", None, ZERO, j["evidence_sha256"], _cells(cells)))
    return cutoff, tuple(sorted(out, key=lambda u: u.journal_id))


def _cells(cells: dict[tuple[str, str], Decimal]) -> tuple[tuple[str, str, Decimal], ...]:
    return tuple(sorted((c, p, n) for (c, p), n in cells.items()))


def bind_and_parse(
    cls: str,
    raw: object,
    side_file_sha: str,
    receipt: dict[str, Any],
    as_of: date,
    codes: dict[str, str],
) -> tuple[Unit, ...]:
    """The side file's sha must equal the import receipt's ``history_cutoff_census.<section>.side_file_sha256`` and its
    cutoff the receipt's and the opening's. (The receipt's own sha is pinned by the caller.)"""
    from erp_v2.migration_ledger.opening import parse_import_receipt

    receipt_cutoff = parse_import_receipt(receipt)
    census = receipt.get("history_cutoff_census")
    entry = census.get(_SECTION[cls]) if isinstance(census, dict) else None
    if not isinstance(entry, dict) or entry.get("side_file_sha256") != side_file_sha:
        refuse("unit_side_file_not_bound_to_receipt")
    cutoff, units = parse_side_file(raw, cls, codes)
    if not (cutoff == receipt_cutoff == as_of):
        refuse("unit_cutoff_mismatch")
    return units


def bridge_items(units: tuple[Unit, ...], rounding_code: str | None = None) -> tuple[Item, ...]:
    """Line items (legacy = v2) per unit and (account, period); E15 adds the residue item on the rounding account."""
    from erp_v2.migration_ledger.bridge import Item

    items: list[Item] = []
    for u in units:
        rule = _RULE[u.cls]
        prefix = "subkobo" if u.cls == E15 else "straddle"
        for code, period, net in u.cells:
            items.append(
                Item(f"{prefix}:{u.journal_id}:{code}:{period}", "transform", u.journal_id, code, period, rule,
                     "line", net, net, u.evidence_sha256)
            )  # fmt: skip
        if u.cls == E15:
            if rounding_code is None:
                refuse("rounding_account_required", "Finance must name the rounding account")
            items.append(
                Item(f"{prefix}:{u.journal_id}:{RESIDUE}", "transform", u.journal_id, rounding_code, u.period, rule,
                     "line", ZERO, u.residue, u.evidence_sha256)
            )  # fmt: skip
    return tuple(items)


def require_items_equal(rule: str, actual: tuple[Item, ...], expected: tuple[Item, ...]) -> None:
    """The ``rule`` items of the bridge inputs must be exactly the derived set (no more, no fewer, no edits)."""
    mine = sorted((i for i in actual if i.rule == rule), key=lambda i: i.item_id)
    if [i.as_dict() for i in mine] != [i.as_dict() for i in sorted(expected, key=lambda i: i.item_id)]:
        refuse("unit_items_forged", f"{rule} items must equal the set derived from the receipt-bound side file")


def residue_by_period(units: tuple[Unit, ...]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for u in units:
        if u.cls == E15:
            out[u.period] = out.get(u.period, ZERO) + u.residue
    return out


def summary(units: tuple[Unit, ...], rounding_code: str | None = None) -> dict[str, Any]:
    return {
        "journals": len(units),
        "rounding_account_code": rounding_code,
        "residue_by_period": {p: fmt(v) for p, v in sorted(residue_by_period(units).items())},
        "items": [{"journal_id": u.journal_id, "type": u.kind} for u in units],
    }
