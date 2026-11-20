"""E15 sub-kobo journals: the T-SUBKOBO bridge items (E15-AND-SIDEFILE-RULING 2026-11-20; the B-9r share).

Three unbalanced legacy journals (sub-kobo residue) are refused as units by the importer (B-13, class E15) and
reproduced by B-14 as ONE ``migration_subkobo`` system journal each: the legacy ledger lines EXACTLY (6 dp) plus one
residue line on ``migration.rounding_account``. After that v2 equals legacy on every account the journals touch, and
the only cell that differs is (rounding account, period of the journal day) by the sum of the residues.

This module derives, from the sha-bound B-13 side file (``<receipt>.subkobo.json``), the ``T-SUBKOBO`` items of each
journal: one RESIDUE item on the rounding-account cell (legacy 0, v2 = the residue line's net, ``-net``) and, because a
ref may sit in only one bucket and the importer did not land these journals, one line item per (account, period) of
its legacy lines (legacy = v2 = the net), so the journal is wholly covered by this rule. ``bridge.compute`` refuses
unless the items in its inputs equal this set exactly and the residue items sum to the rounding-cell difference. The
bridge inputs must therefore carry NO other item for these refs.

Side file (exact key set): ``{history_cutoff, class: "E15", reason: "unbalanced_journal_unit", journals}``; per journal
the #74 ``as_dict`` keys; per line ``{ledger_line_id, journal_line_id, account_id, account_code, posting_date, debit,
credit, original_currency}``; ``evidence_sha256 = sha256(json.dumps(lines, sort_keys=True))``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from erp_v2.migration_ledger.common import ZERO, day, fmt, keys_exactly, money, refuse, sha_text, text

if TYPE_CHECKING:
    from erp_v2.migration_ledger.bridge import Item

SUBKOBO_RULE: Final = "T-SUBKOBO"
E15: Final = "E15"
RESIDUE: Final = "residue"
REASON: Final = "unbalanced_journal_unit"
KIND: Final = "migration_subkobo"
LIMIT: Final = Decimal("0.01")
_TOP = {"history_cutoff", "class", "reason", "journals"}
_JOURNAL = {
    "class", "reason", "journal", "type", "legacy_status", "journal_date", "source_module", "source_document_type",
    "source_document_id", "currency", "lines", "evidence_sha256", "post_net", "net",
}  # fmt: skip
_LINE = {
    "ledger_line_id", "journal_line_id", "account_id", "account_code", "posting_date", "debit", "credit",
    "original_currency",
}  # fmt: skip


@dataclass(frozen=True)
class SubkoboJournal:
    journal_id: str
    day: date
    net: Decimal  # sum(debit) - sum(credit) of the legacy lines; the residue line posts -net
    evidence_sha256: str
    cells: tuple[tuple[str, str, Decimal], ...] = ()  # (account code, YYYY-MM, net) of the legacy lines

    @property
    def period(self) -> str:
        return self.day.strftime("%Y-%m")

    @property
    def residue(self) -> Decimal:
        return -self.net


def unit_evidence_sha(lines: list[Any]) -> str:
    """Exactly #74's ``classify_straddlers`` computation (to be shared as ``history_cutoff.unit_evidence_sha``)."""
    return hashlib.sha256(json.dumps(lines, sort_keys=True).encode()).hexdigest()


def parse_side_file(raw: object) -> tuple[date, tuple[SubkoboJournal, ...]]:
    body = keys_exactly(raw, _TOP, "invalid_subkobo_side_file")
    if body["class"] != E15 or body["reason"] != REASON or not isinstance(body["journals"], list):
        refuse("invalid_subkobo_side_file")
    cutoff = day(body["history_cutoff"], "invalid_subkobo_side_file")
    out: list[SubkoboJournal] = []
    seen: set[str] = set()
    for entry in body["journals"]:
        j = keys_exactly(entry, _JOURNAL, "invalid_subkobo_journal")
        if j["class"] != E15 or j["reason"] != REASON or j["type"] != "UNBALANCED":
            refuse("invalid_subkobo_journal")
        journal = text(j["journal"], "invalid_subkobo_journal", 255)
        if journal in seen:
            refuse("subkobo_duplicate_journal")
        seen.add(journal)
        lines = j["lines"]
        if not isinstance(lines, list) or not lines:
            refuse("invalid_subkobo_journal")
        if unit_evidence_sha(lines) != sha_text(j["evidence_sha256"], "invalid_subkobo_journal"):
            refuse("evidence_sha_mismatch")
        days: set[date] = set()
        net = ZERO
        cells: dict[tuple[str, str], Decimal] = {}
        for ln in lines:
            e = keys_exactly(ln, _LINE, "invalid_subkobo_line")
            when = day(e["posting_date"], "invalid_subkobo_line")
            if when <= cutoff:
                refuse("subkobo_splits_cutoff")
            days.add(when)
            amount = money(e["debit"], "invalid_subkobo_line") - money(e["credit"], "invalid_subkobo_line")
            net += amount
            code = text(e["account_code"], "invalid_subkobo_line", 30)
            cells[(code, when.strftime("%Y-%m"))] = cells.get((code, when.strftime("%Y-%m")), ZERO) + amount
        if len(days) != 1:
            refuse("subkobo_multi_day")
        if money(j["net"], "invalid_subkobo_journal") != net or money(j["post_net"], "invalid_subkobo_journal") != net:
            refuse("subkobo_net_differs_from_lines")  # recomputed from the lines, never taken from the file
        if not ZERO < abs(net) < LIMIT:
            refuse("subkobo_residue_out_of_range")
        out.append(
            SubkoboJournal(
                journal,
                next(iter(days)),
                net,
                j["evidence_sha256"],
                tuple(sorted((c, p, n) for (c, p), n in cells.items())),
            )
        )
    return cutoff, tuple(sorted(out, key=lambda s: s.journal_id))


def bind_and_parse(raw: object, side_file_sha: str, receipt: dict[str, Any], as_of: date) -> tuple[SubkoboJournal, ...]:
    """The side file's sha must equal the import receipt's ``history_cutoff_census.subkobo.side_file_sha256`` and its
    cutoff the receipt's and the opening's. A side-file sha alone is not enough."""
    from erp_v2.migration_ledger.opening import parse_import_receipt

    receipt_cutoff = parse_import_receipt(receipt)
    census = receipt.get("history_cutoff_census")
    entry = census.get("subkobo") if isinstance(census, dict) else None
    if not isinstance(entry, dict) or entry.get("side_file_sha256") != side_file_sha:
        refuse("subkobo_side_file_not_bound_to_receipt")
    cutoff, journals = parse_side_file(raw)
    if not (cutoff == receipt_cutoff == as_of):
        refuse("subkobo_cutoff_mismatch")
    return journals


def bridge_items(journals: tuple[SubkoboJournal, ...], rounding_code: str | None) -> tuple[Item, ...]:
    """Per journal: the RESIDUE item on (rounding account, journal period) (legacy 0, v2 = the residue line) and one
    line item per (account, period) of its legacy lines (legacy = v2)."""
    from erp_v2.migration_ledger.bridge import Item

    if journals and rounding_code is None:
        refuse("rounding_account_required", "Finance must name the rounding account")
    items: list[Item] = []
    for j in journals:
        for code, period, net in j.cells:
            items.append(
                Item(f"subkobo:{j.journal_id}:{code}:{period}", "transform", j.journal_id, code, period,
                     SUBKOBO_RULE, "line", net, net, j.evidence_sha256)
            )  # fmt: skip
        items.append(
            Item(f"subkobo:{j.journal_id}:{RESIDUE}", "transform", j.journal_id, rounding_code, j.period,  # type: ignore[arg-type]
                 SUBKOBO_RULE, "line", ZERO, j.residue, j.evidence_sha256)
        )  # fmt: skip
    return tuple(items)


def require_items_equal(actual: tuple[Item, ...], expected: tuple[Item, ...]) -> None:
    """The T-SUBKOBO items of the bridge inputs must be exactly the derived set, and sum to the residues per period."""
    mine = sorted((i for i in actual if i.rule == SUBKOBO_RULE), key=lambda i: i.item_id)
    if [i.as_dict() for i in mine] != [i.as_dict() for i in sorted(expected, key=lambda i: i.item_id)]:
        refuse("subkobo_items_forged", "T-SUBKOBO items must equal the set derived from the sha-bound side file")


def residue_by_period(journals: tuple[SubkoboJournal, ...]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for j in journals:
        out[j.period] = out.get(j.period, ZERO) + j.residue
    return out


def summary(journals: tuple[SubkoboJournal, ...], rounding_code: str | None) -> dict[str, Any]:
    return {
        "journals": len(journals),
        "rounding_account_code": rounding_code,
        "residue_by_period": {p: fmt(v) for p, v in sorted(residue_by_period(journals).items())},
        "items": [{"journal_id": j.journal_id, "period": j.period, "residue": fmt(j.residue)} for j in journals],
    }
