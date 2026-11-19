"""Per-customer attribution of the legacy 1400 GL: the census receipt fields B-14 consumes (B14-B15-SPEC B14.4).

Pure over already-read rows (the read-only loader is `load_gl_rows`). Every legacy 1400 ledger line belongs to a
journal; a journal is attributed to the (customer, currency) of the legacy invoice / payment that posted it (and of
its reversal journal). The opening line is the cumulative legacy balance to D, so attributing ALL legacy lines the
same way includes each customer's share of the opening line. Outcomes per journal:

- exactly one owner: its net is that customer's `gl_minor` (debit - credit, minor units, signed);
- no owner: `unattributed[]` (E36a `gljournal:`);
- two or more owners (`shared_ar_journal`): NOT split. It is listed in `shared_ar_journal[]` and ALSO included in
  `unattributed[]` with `"shared": true`, so that the invariant B-14 checks is exact:
      sum(gl_minor) + sum(unattributed) == the legacy 1400 net (== v2 GL(1400)), per currency.
  `shared_ar_journal[]` is detail only and must not be summed again.

Per party the receipt also carries `carried_minor` (open items admitted to billing), `cash_lot_minor` (cash-lot
residuals admitted), the `held` GL-signed items that explain the party's refused amounts, and `custgap_minor`
(the unexplained remainder: gl - (carried - cash_lot) - sum(held)); a nonzero remainder is an E36a `custgap:` item.
Sign convention [I]: receivable debit positive; an unposted DRAFT/SUBMITTED document carries no GL (amount 0).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal

from erp_v2.importer.ar_census import (
    CARRY_ITEM,
    CARRY_LOT,
    EXCLUDED_PARTITION,
    NOT_RECEIVABLE,
    UNAPPLIED_CREDIT_NOTE,
    ArCensus,
    CensusError,
)

PartyKey = tuple[str, str]  # (legacy customer key, currency)


def _minor(amount: object, journal: str, precision: int = 2) -> int:
    scaled = Decimal(str(amount)) * (Decimal(10) ** precision)
    if scaled != scaled.to_integral_value():
        raise CensusError(f"gl_amount_not_exact:{journal}")  # fail closed: never rounded into an attribution
    return int(scaled)


def _held_amount(row, invoice_types: Mapping[str, str]) -> int:
    """GL-signed amount of a row kept out of billing: invoice receivable +, unapplied cash -, a positive-valued
    credit note reduces the receivable (-)."""

    if row.kind == "payment":
        return -(row.residual_minor or 0)
    residual = row.residual_minor or 0
    if invoice_types.get(row.key, "").upper() == "CREDIT_NOTE" and (row.gross_minor or 0) > 0:
        return -residual
    return residual


def build_attribution(
    census: ArCensus,
    *,
    gl_lines: Iterable[tuple[str, object]],
    journal_owners: Mapping[str, Iterable[PartyKey]],
    invoice_types: Mapping[str, str],
    functional_currency: str,
) -> dict[str, object]:
    """`gl_lines`: (journal id, net debit - credit as Decimal-like) per legacy 1400 line or journal (summed here)."""

    per_journal: dict[str, int] = defaultdict(int)
    for journal, net in gl_lines:
        per_journal[str(journal)] += _minor(net, str(journal))
    gl: dict[PartyKey, int] = defaultdict(int)
    unattributed: dict[str, list[dict[str, object]]] = defaultdict(list)
    shared: list[dict[str, object]] = []
    for journal in sorted(per_journal):
        amount = per_journal[journal]
        owners = sorted({(str(c), str(k)) for c, k in journal_owners.get(journal, ())})
        if len(owners) == 1:
            gl[owners[0]] += amount
        elif not owners:
            unattributed[functional_currency].append({"gljournal_id": journal, "amount_minor": amount})
        else:
            shared.append(
                {
                    "gljournal_id": journal,
                    "amount_minor": amount,
                    "partitions": [list(o) for o in owners],
                    "currency": functional_currency,
                }
            )
            unattributed[functional_currency].append({"gljournal_id": journal, "amount_minor": amount, "shared": True})

    carried: dict[PartyKey, int] = defaultdict(int)
    lots: dict[PartyKey, int] = defaultdict(int)
    held: dict[PartyKey, list[dict[str, object]]] = defaultdict(list)
    for row in census.rows:
        if row.partition is None:
            continue  # unposted documents (no partition, no GL) are listed below
        party = row.partition
        if row.bucket == CARRY_ITEM:
            carried[party] += row.residual_minor or 0
        elif row.bucket == CARRY_LOT:
            lots[party] += row.residual_minor or 0
        elif row.bucket == UNAPPLIED_CREDIT_NOTE:
            held[party].append(
                {
                    "reason": "ar_unapplied_credit_note",
                    "legacy_key": row.key,
                    "amount_minor": _held_amount(row, invoice_types),
                }
            )
        elif row.bucket == EXCLUDED_PARTITION and (row.residual_minor or 0) != 0:
            held[party].append(
                {
                    "reason": f"partition_held:{row.reason}",
                    "legacy_key": row.key,
                    "amount_minor": _held_amount(row, invoice_types),
                }
            )
    unposted = [
        {"reason": "doc_unposted", "legacy_key": r.key, "amount_minor": 0, "gross_minor": r.gross_minor}
        for r in census.rows
        if r.bucket == NOT_RECEIVABLE and r.reason.startswith("unposted_")
    ]

    parties = sorted(set(gl) | set(carried) | set(lots) | set(held))
    entries = []
    for party in parties:
        explained = sum(int(h["amount_minor"]) for h in held[party])
        sub = carried[party] - lots[party]
        entries.append(
            {
                "customer": party[0],
                "currency": party[1],
                "gl_minor": gl[party],
                "carried_minor": carried[party],
                "cash_lot_minor": lots[party],
                "custgap_minor": gl[party] - sub - explained,
            }
        )
    held_all = [h for party in parties for h in held[party]] + unposted
    # The legacy 1400 net per currency, straight from the journals (independent of the split above).
    totals: dict[str, int] = defaultdict(int)
    for journal, amount in per_journal.items():
        owners = {str(k) for _c, k in journal_owners.get(journal, ())}
        totals[owners.pop() if len(owners) == 1 else functional_currency] += amount
    return {
        "gl_attribution": {
            "parties": entries,
            "held": held_all,
            "unattributed": [i for items in unattributed.values() for i in items],
            "shared_ar_journal": shared,
            "legacy_1400_minor_by_currency": dict(sorted(totals.items())),
        }
    }


# --- read-only loader (the caller sets the READ ONLY transaction posture) ----------------------------------------

OWNER_SQL = """
SELECT i.journal_entry_id::text AS journal_id, i.customer_id::text AS customer_key, i.currency_code
FROM ar.invoice i WHERE i.journal_entry_id IS NOT NULL
UNION ALL
SELECT j.reversal_journal_id::text, i.customer_id::text, i.currency_code
FROM ar.invoice i JOIN gl.journal_entry j ON j.journal_entry_id = i.journal_entry_id
WHERE j.reversal_journal_id IS NOT NULL
UNION ALL
SELECT p.journal_entry_id::text, p.customer_id::text, p.currency_code
FROM ar.customer_payment p WHERE p.journal_entry_id IS NOT NULL
UNION ALL
SELECT j.reversal_journal_id::text, p.customer_id::text, p.currency_code
FROM ar.customer_payment p JOIN gl.journal_entry j ON j.journal_entry_id = p.journal_entry_id
WHERE j.reversal_journal_id IS NOT NULL
"""
GL_SQL = """
SELECT journal_entry_id::text AS journal_id, sum(debit_amount - credit_amount) AS net
FROM gl.posted_ledger_line WHERE account_id::text = ANY(:accounts) GROUP BY 1
"""


def load_gl_rows(source, ar_account_ids: Iterable[str]):
    """(gl lines, journal owners) from the legacy snapshot; `ar_account_ids` = the account coded 1400 (same ids)."""

    import sqlalchemy as sa

    accounts = sorted({str(a) for a in ar_account_ids})
    if not accounts:
        raise CensusError("ar_control_account_missing")
    lines = [(r["journal_id"], r["net"]) for r in source.execute(sa.text(GL_SQL), {"accounts": accounts}).mappings()]
    owners: dict[str, set[PartyKey]] = defaultdict(set)
    for r in source.execute(sa.text(OWNER_SQL)).mappings():
        owners[r["journal_id"]].add((str(r["customer_key"]), str(r["currency_code"])))
    return lines, owners
