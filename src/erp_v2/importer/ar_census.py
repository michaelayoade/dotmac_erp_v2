"""AR opening census: sha-bound, partitioned, exactly-once buckets (AR opening admission rev 2, section 2).

Pure over already-read legacy rows (the read-only loader is `load_legacy_rows`). Every `ar.invoice` and every
`ar.customer_payment` lands in exactly ONE bucket:

- `CARRY_ITEM` / `CARRY_LOT`: in a carryable customer x currency partition (a receivable item / a cash lot);
- `UNAPPLIED_CREDIT_NOTE(reason)`: an unapplied credit note (no allocation links, nothing depends on it). It is NOT part
  of the billing manifest (billing a13 cannot hold a row inside a partition: P-1 = NO) and does not stop its
  partition from being carried; it is a work-queue item (`ar_unapplied_credit_note`) and part of the 1400 holding;
- `EXCLUDED_PARTITION(reason)`: its partition has a DEPENDENCY blocker, so the WHOLE partition is excluded (E35);
- `NOT_RECEIVABLE`: DRAFT or SUBMITTED, or issued after the capture cutoff (no GL, Finance re-issues or voids);
- `CLOSED`: residual 0 (history, no item), or not posted at the cutoff and not a draft.

"Posted by D" and the allocation rules of MIGRATION-REVIEWED-CONVERSION 12.3b-addendum are applied by the loader
(it sets `posted` and filters allocations to `allocation_date <= cutoff` whose payment is posted by the cutoff);
the census trusts those flags only to bucket, then re-proves the partition: every id exactly once, and totals by
origin x bucket tie to the source totals. The census sha256 binds snapshot sha, cutoff, every bucket and every
excluded partition's blocker codes, so ERP = census = params.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from dotmac_billing.contracts import PartyDocumentSnapshotV1, PaymentInstructionsSnapshotV1
from dotmac_billing.opening_contracts import OpeningPartitionV1

from erp_v2.importer.billing_opening import (
    ORIGINS,
    OpeningTranslationContext,
    OpeningTranslationError,
    origin_of,
    translate_partition,
)

CARRY_ITEM = "CARRY_ITEM"
CARRY_LOT = "CARRY_LOT"
EXCLUDED_PARTITION = "EXCLUDED_PARTITION"
UNAPPLIED_CREDIT_NOTE = "UNAPPLIED_CREDIT_NOTE"
NOT_RECEIVABLE = "NOT_RECEIVABLE"
CLOSED = "CLOSED"
BUCKETS = (CARRY_ITEM, CARRY_LOT, EXCLUDED_PARTITION, UNAPPLIED_CREDIT_NOTE, NOT_RECEIVABLE, CLOSED)
UNPOSTED_STATUSES = frozenset({"DRAFT", "SUBMITTED"})
_SHA = re.compile(r"[0-9a-f]{64}")

PartitionKey = tuple[str, str]  # (legacy customer key, currency)


class CensusError(RuntimeError):
    """The census cannot be formed or does not prove its partition."""


@dataclass(frozen=True)
class CensusRow:
    kind: str  # invoice | payment
    key: str
    bucket: str
    reason: str
    origin: str
    partition: PartitionKey | None
    gross_minor: int | None
    residual_minor: int | None
    document_number: str | None = None  # only set for unapplied credit notes (work-queue item detail)


@dataclass(frozen=True)
class ExcludedPartition:
    partition: PartitionKey
    blockers: tuple[tuple[str, str], ...]  # (source key, reason), sorted
    open_residual_minor: int
    invoice_keys: tuple[str, ...]
    payment_keys: tuple[str, ...]


@dataclass(frozen=True)
class CensusVariance:
    """A stored-balance difference carried into the manifest (billing records it at import or seal): an E36b item."""

    partition: PartitionKey
    source_key: str
    reason: str
    amount_minor: int


@dataclass(frozen=True)
class ArCensus:
    snapshot_sha256: str
    cutoff: str
    rows: tuple[CensusRow, ...]
    carry_partitions: tuple[PartitionKey, ...]
    excluded: tuple[ExcludedPartition, ...]
    sha256: str
    variances: tuple[CensusVariance, ...] = ()
    credit_note_only_partitions: tuple[
        PartitionKey, ...
    ] = ()  # only unapplied credit notes: nothing to carry, no account minted

    def totals(self) -> dict[str, dict[str, dict[str, int]]]:
        """origin -> bucket -> {count, gross_minor, residual_minor} (inexact amounts counted as 0, listed apart)."""

        out: dict[str, dict[str, dict[str, int]]] = {o: {} for o in ORIGINS}
        for row in self.rows:
            cell = out[row.origin].setdefault(row.bucket, {"count": 0, "gross_minor": 0, "residual_minor": 0})
            cell["count"] += 1
            cell["gross_minor"] += row.gross_minor or 0
            cell["residual_minor"] += row.residual_minor or 0
        return out

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(BUCKETS, 0)
        for row in self.rows:
            counts[row.bucket] += 1
        return counts

    def receipt(self) -> dict[str, object]:
        """Counts and amounts only (no names): the 0600 census receipt body."""

        return {
            "kind": "ar-census",
            "snapshot_sha256": self.snapshot_sha256,
            "cutoff": self.cutoff,
            "census_sha256": self.sha256,
            "buckets": self.counts(),
            "totals_by_origin": self.totals(),
            "carry_partitions": len(self.carry_partitions),
            "credit_note_only_partitions": [list(p) for p in self.credit_note_only_partitions],
            "unapplied_credit_notes": [
                {
                    "kind": r.kind,
                    "key": r.key,
                    "reason": r.reason,
                    "document_number": r.document_number,
                    "partition": list(r.partition) if r.partition else None,
                    "gross_minor": r.gross_minor,
                    "residual_minor": r.residual_minor,
                }
                for r in self.rows
                if r.bucket == UNAPPLIED_CREDIT_NOTE
            ],
            "unposted_documents": [
                {
                    "kind": r.kind,
                    "key": r.key,
                    "status": r.reason.removeprefix("unposted_").upper(),
                    "gross_minor": r.gross_minor,
                }
                for r in self.rows
                if r.bucket == NOT_RECEIVABLE and r.reason.startswith("unposted_")
            ],
            "variances": [
                {
                    "partition": list(v.partition),
                    "source_key": v.source_key,
                    "reason": v.reason,
                    "amount_minor": v.amount_minor,
                }
                for v in self.variances
            ],
            "excluded_partitions": [
                {
                    "partition": list(e.partition),
                    "blockers": [list(b) for b in e.blockers],
                    "open_residual_minor": e.open_residual_minor,
                }
                for e in self.excluded
            ],
        }


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def _minor(amount: object, precision: int) -> int | None:
    scaled = Decimal(str(amount)) * (Decimal(10) ** precision)
    return int(scaled) if scaled == scaled.to_integral_value() else None


def _as_dt(value: object) -> datetime:
    """Legacy date columns arrive as date or datetime; compare and translate as UTC datetimes."""

    from datetime import UTC, date

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raise CensusError(f"not a date: {value!r}")


def normalise_rows(
    invoices: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
    allocations: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Date-typed legacy columns become UTC datetimes once, so every consumer sees one shape."""

    return (
        [{**i, "invoice_date": _as_dt(i["invoice_date"])} for i in invoices],
        [{**p, "payment_date": _as_dt(p["payment_date"])} for p in payments],
        [{**a, "allocated_at": _as_dt(a["allocated_at"])} for a in allocations],
    )


def partition_rows(
    part: PartitionKey,
    invoices: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
    allocations: Sequence[Mapping[str, object]],
    cutoff: datetime,
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]], list[Mapping[str, object]]]:
    """The in-play rows of one customer x currency partition (the SAME selection the census used).

    In play = invoice posted by the cutoff and issued on or before it; payment posted by the cutoff and dated on or
    before it. Allocations of payments in play, plus allocations onto this partition's invoices by payments
    of another partition (the translator refuses those as outside the population).
    """

    cutoff = _as_dt(cutoff)
    invs = [
        i
        for i in invoices
        if i.get("posted")
        and _as_dt(i["invoice_date"]) <= cutoff
        and (str(i["customer_key"]), str(i["currency_code"])) == part
    ]
    pays = [
        p
        for p in payments
        if p.get("posted")
        and _as_dt(p["payment_date"]) <= cutoff
        and (str(p["customer_key"]), str(p["currency_code"])) == part
    ]
    play_payments = {str(p["payment_id"]) for p in payments if p.get("posted") and _as_dt(p["payment_date"]) <= cutoff}
    mine = {str(p["payment_id"]) for p in pays}
    inv_ids = {str(i["invoice_id"]) for i in invs}
    allocs = [
        a
        for a in allocations
        if str(a["payment_id"]) in play_payments and (str(a["payment_id"]) in mine or str(a["invoice_id"]) in inv_ids)
    ]
    return invs, pays, allocs


def _stub_context(
    key: PartitionKey,
    precision: int,
    customer_snapshot: Callable[[str], PartyDocumentSnapshotV1 | None],
    seller: PartyDocumentSnapshotV1,
) -> OpeningTranslationContext:
    # Classification only: the placeholder account id never reaches a manifest (the plan rebuilds with real ids).
    from uuid import NAMESPACE_URL, uuid5

    return OpeningTranslationContext(
        partition=OpeningPartitionV1(
            source_account_key=key[0],
            account_id=uuid5(NAMESPACE_URL, f"ar-census-placeholder:{key[0]}:{key[1]}"),
            currency=key[1],
            precision=precision,
        ),
        seller=seller,
        customer_snapshot=customer_snapshot,
        payment_instructions=PaymentInstructionsSnapshotV1(method_code="bank_transfer"),
    )


def build_census(
    *,
    snapshot_sha256: str,
    cutoff: datetime,
    invoices: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
    allocations: Sequence[Mapping[str, object]],
    customer_snapshot: Callable[[str], PartyDocumentSnapshotV1 | None],
    seller: PartyDocumentSnapshotV1,
    precision: int = 2,
) -> ArCensus:
    """Bucket every legacy invoice and payment exactly once and prove it.

    Row contract (the loader's): invoices carry `posted` (bool, "posted by the cutoff"), `status`,
    `invoice_date`; payments carry `posted`; allocations are already cut off. Missing provenance columns refuse
    (`translate_partition` raises `OpeningTranslationError`); duplicate ids raise `CensusError`.
    """

    cutoff = _as_dt(cutoff)
    invoices, payments, allocations = normalise_rows(invoices, payments, allocations)
    if not _SHA.fullmatch(snapshot_sha256):
        raise CensusError("snapshot_sha256 must be 64 lowercase hex chars")
    seen_invoices: set[str] = set()
    seen_payments: set[str] = set()
    for row in invoices:
        key = str(row["invoice_id"])
        if key in seen_invoices:
            raise CensusError(f"duplicate invoice id {key}")
        seen_invoices.add(key)
    for row in payments:
        key = str(row["payment_id"])
        if key in seen_payments:
            raise CensusError(f"duplicate payment id {key}")
        seen_payments.add(key)

    out: dict[tuple[str, str], CensusRow] = {}

    def put(row: CensusRow) -> None:
        if (row.kind, row.key) in out:  # exactly-once guard
            raise CensusError(f"{row.kind} {row.key} bucketed twice")
        out[(row.kind, row.key)] = row

    in_play_invoices: dict[PartitionKey, list[Mapping[str, object]]] = defaultdict(list)
    in_play_payments: dict[PartitionKey, list[Mapping[str, object]]] = defaultdict(list)
    for inv in invoices:
        key = str(inv["invoice_id"])
        for required in ("dotmac_sub_id", "splynx_id"):
            if required not in inv:
                raise OpeningTranslationError(f"invoice {key}: row lacks {required}; refusing")
        origin = origin_of(inv)
        gross = _minor(inv["total_amount"], precision)
        status = str(inv.get("status", "")).upper()
        issued = _as_dt(inv["invoice_date"])
        if not inv.get("posted"):
            bucket, reason = (
                (NOT_RECEIVABLE, f"unposted_{status.lower()}")
                if status in UNPOSTED_STATUSES
                else (CLOSED, "not_posted_at_cutoff")
            )
            put(CensusRow("invoice", key, bucket, reason, origin, None, gross, None))
        elif issued > cutoff:
            put(CensusRow("invoice", key, NOT_RECEIVABLE, "after_cutoff", origin, None, gross, None))
        else:
            in_play_invoices[(str(inv["customer_key"]), str(inv["currency_code"]))].append(inv)
    for pay in payments:
        key = str(pay["payment_id"])
        for required in ("dotmac_sub_id", "splynx_id"):
            if required not in pay:
                raise OpeningTranslationError(f"payment {key}: row lacks {required}; refusing")
        origin = origin_of(pay)
        gross = _minor(pay["amount"], precision)
        if not pay.get("posted") or _as_dt(pay["payment_date"]) > cutoff:
            put(CensusRow("payment", key, CLOSED, "not_posted_at_cutoff", origin, None, gross, None))
        else:
            in_play_payments[(str(pay["customer_key"]), str(pay["currency_code"]))].append(pay)

    partitions = sorted(set(in_play_invoices) | set(in_play_payments))
    carry: list[PartitionKey] = []
    credit_note_only: list[PartitionKey] = []
    variances: list[CensusVariance] = []
    excluded: list[ExcludedPartition] = []
    for part in partitions:
        invs, pays, part_allocs = partition_rows(part, invoices, payments, allocations, cutoff)
        translation = translate_partition(
            context=_stub_context(part, precision, customer_snapshot, seller),
            invoices=invs,
            allocations=part_allocs,
            payments=pays,
        )
        closed_inv = set(translation.closed_invoices)
        ignored = set(translation.ignored_payments)
        credit_by_key = {h.source_key: h.reason for h in translation.unapplied_credit_notes}
        if translation.carryable and credit_by_key and not translation.items and not translation.cash_lots:
            credit_note_only.append(part)  # only unapplied credit notes: nothing to carry
        elif translation.carryable:
            carry.append(part)
        if translation.carryable:
            variances.extend(
                CensusVariance(part, v.source.key, v.reason_code, v.amount_minor) for v in translation.variances
            )
        else:
            by_key: dict[str, list[str]] = defaultdict(list)
            for blocker in translation.blockers:
                by_key[blocker.source_key].append(blocker.reason)
            excluded.append(
                ExcludedPartition(
                    partition=part,
                    blockers=tuple(sorted({(b.source_key, b.reason) for b in translation.blockers})),
                    open_residual_minor=sum(
                        r for k, r in translation.residual_minor_by_invoice.items() if k not in closed_inv
                    ),
                    invoice_keys=tuple(
                        sorted(str(i["invoice_id"]) for i in invs if str(i["invoice_id"]) not in closed_inv)
                    ),
                    payment_keys=tuple(
                        sorted(str(p["payment_id"]) for p in pays if str(p["payment_id"]) not in ignored)
                    ),
                )
            )
        for inv in invs:
            key = str(inv["invoice_id"])
            origin = origin_of(inv)
            gross = translation.gross_minor_by_key.get(key, _minor(inv["total_amount"], precision))
            residual = translation.residual_minor_by_invoice.get(key)
            if key in credit_by_key:
                row = CensusRow(
                    "invoice",
                    key,
                    UNAPPLIED_CREDIT_NOTE,
                    credit_by_key[key],
                    origin,
                    part,
                    gross,
                    residual,
                    str(inv.get("invoice_number") or ""),
                )
            elif key in closed_inv:
                row = CensusRow("invoice", key, CLOSED, "residual_zero", origin, part, gross, 0)
            elif translation.carryable:
                row = CensusRow("invoice", key, CARRY_ITEM, "translated", origin, part, gross, residual)
            else:
                reasons = sorted(set(by_key.get(key, [])) or {"partition_excluded"})
                row = CensusRow("invoice", key, EXCLUDED_PARTITION, ",".join(reasons), origin, part, gross, residual)
            put(row)
        for pay in pays:
            key = str(pay["payment_id"])
            origin = origin_of(pay)
            gross = translation.gross_minor_by_key.get(key, _minor(pay["amount"], precision))
            if key in ignored:
                row = CensusRow("payment", key, CLOSED, "fully_allocated_to_history", origin, part, gross, 0)
            elif translation.carryable:
                row = CensusRow(
                    "payment",
                    key,
                    CARRY_LOT,
                    "translated",
                    origin,
                    part,
                    gross,
                    translation.payment_residual_minor.get(key),
                )
            else:
                reasons = sorted(set(by_key.get(key, [])) or {"partition_excluded"})
                row = CensusRow(
                    "payment",
                    key,
                    EXCLUDED_PARTITION,
                    ",".join(reasons),
                    origin,
                    part,
                    gross,
                    translation.payment_residual_minor.get(key),
                )
            put(row)

    rows = tuple(sorted(out.values(), key=lambda r: (r.kind, r.key)))
    carry_t, excluded_t = tuple(sorted(carry)), tuple(sorted(excluded, key=lambda e: e.partition))
    digest = hashlib.sha256(
        _canonical(
            {
                "domain": "erp.ar-census.v1",
                "snapshot_sha256": snapshot_sha256,
                "cutoff": cutoff.isoformat(),
                "rows": [
                    [
                        r.kind,
                        r.key,
                        r.bucket,
                        r.reason,
                        r.origin,
                        list(r.partition) if r.partition else None,
                        r.gross_minor,
                        r.residual_minor,
                        r.document_number,
                    ]
                    for r in rows
                ],
                "excluded": [[list(e.partition), [list(b) for b in e.blockers]] for e in excluded_t],
                "credit_note_only": [list(p) for p in sorted(credit_note_only)],
                "variances": [
                    [list(v.partition), v.source_key, v.reason, v.amount_minor]
                    for v in sorted(variances, key=lambda v: (v.partition, v.source_key))
                ],
            }
        )
    ).hexdigest()
    census = ArCensus(
        snapshot_sha256,
        cutoff.isoformat(),
        rows,
        carry_t,
        excluded_t,
        digest,
        tuple(sorted(variances, key=lambda v: (v.partition, v.source_key))),
        tuple(sorted(credit_note_only)),
    )
    prove_partition(census, invoice_ids=seen_invoices, payment_ids=seen_payments, invoices=invoices, payments=payments)
    return census


def prove_partition(
    census: ArCensus,
    *,
    invoice_ids: set[str],
    payment_ids: set[str],
    invoices: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
    precision: int = 2,
) -> None:
    """Every id exactly once across buckets; totals by origin tie to the source totals. Raises CensusError."""

    bucketed_inv = [r.key for r in census.rows if r.kind == "invoice"]
    bucketed_pay = [r.key for r in census.rows if r.kind == "payment"]
    if len(bucketed_inv) != len(set(bucketed_inv)) or set(bucketed_inv) != invoice_ids:
        raise CensusError("invoice ids are not bucketed exactly once")
    if len(bucketed_pay) != len(set(bucketed_pay)) or set(bucketed_pay) != payment_ids:
        raise CensusError("payment ids are not bucketed exactly once")
    if any(r.bucket not in BUCKETS for r in census.rows):
        raise CensusError("unknown bucket")
    for kind, rows, field in (("invoice", invoices, "total_amount"), ("payment", payments, "amount")):
        source: dict[str, int] = dict.fromkeys(ORIGINS, 0)
        for row in rows:
            source[origin_of(row)] += _minor(row[field], precision) or 0
        bucketed: dict[str, int] = dict.fromkeys(ORIGINS, 0)
        for r in census.rows:
            if r.kind == kind:
                bucketed[r.origin] += r.gross_minor or 0
        if source != bucketed:
            raise CensusError(f"{kind} totals by origin do not tie to the source")


# --- read-only loader over the legacy snapshot (LEGACY_DATABASE_URL), shape per 12.3b-addendum ------------------

INVOICE_SQL = """
SELECT i.invoice_id, i.invoice_number, i.customer_id AS customer_key, i.invoice_date, i.due_date,
       i.currency_code, i.subtotal, i.tax_amount, i.total_amount, i.balance_due, i.status, i.invoice_type,
       i.dotmac_sub_id, i.splynx_id,
       (j.journal_entry_id IS NOT NULL) AS posted
FROM ar.invoice i
LEFT JOIN gl.journal_entry j ON j.journal_entry_id = i.journal_entry_id
 AND j.status IN ('POSTED', 'REVERSED') AND j.posting_date <= :cutoff
 AND NOT EXISTS (SELECT 1 FROM gl.journal_entry r WHERE r.journal_entry_id = j.reversal_journal_id
                 AND r.status IN ('POSTED', 'REVERSED') AND r.posting_date <= :cutoff)
"""
PAYMENT_SQL = """
SELECT p.payment_id, p.customer_id AS customer_key, p.gross_amount AS amount, p.payment_date, p.reference,
       p.currency_code, p.status, p.dotmac_sub_id, p.splynx_id,
       (j.journal_entry_id IS NOT NULL) AS posted
FROM ar.customer_payment p
LEFT JOIN gl.journal_entry j ON j.journal_entry_id = p.journal_entry_id
 AND j.status IN ('POSTED', 'REVERSED') AND j.posting_date <= :cutoff
 AND NOT EXISTS (SELECT 1 FROM gl.journal_entry r WHERE r.journal_entry_id = j.reversal_journal_id
                 AND r.status IN ('POSTED', 'REVERSED') AND r.posting_date <= :cutoff)
"""
ALLOCATION_SQL = """
SELECT a.payment_id, a.invoice_id,
       (a.allocated_amount + coalesce(a.discount_taken, 0) + coalesce(a.write_off_amount, 0)) AS amount_allocated,
       a.allocation_date AS allocated_at
FROM ar.payment_allocation a
WHERE a.allocation_date <= :cutoff
"""


def load_legacy_rows(source, cutoff: datetime) -> dict[str, list[dict[str, object]]]:
    """Read the three legacy tables on a READ ONLY connection (the caller sets the transaction posture).

    Allocations whose payment is not posted by the cutoff are dropped here so a discharge only counts when its
    payment counted. Column names are the legacy ERP's; a missing column raises from the driver (fail closed).
    """

    import sqlalchemy as sa

    params = {"cutoff": cutoff.date()}
    invoices = [dict(r) for r in source.execute(sa.text(INVOICE_SQL), params).mappings()]
    payments = [dict(r) for r in source.execute(sa.text(PAYMENT_SQL), params).mappings()]
    posted_payments = {str(p["payment_id"]) for p in payments if p["posted"]}
    allocations = [
        dict(r)
        for r in source.execute(sa.text(ALLOCATION_SQL), params).mappings()
        if str(r["payment_id"]) in posted_payments
    ]
    return {"invoices": invoices, "payments": payments, "allocations": allocations}


__all__ = [
    "BUCKETS",
    "CARRY_ITEM",
    "CARRY_LOT",
    "CLOSED",
    "EXCLUDED_PARTITION",
    "NOT_RECEIVABLE",
    "UNAPPLIED_CREDIT_NOTE",
    "ArCensus",
    "CensusError",
    "CensusRow",
    "CensusVariance",
    "ExcludedPartition",
    "build_census",
    "load_legacy_rows",
    "normalise_rows",
    "partition_rows",
    "prove_partition",
]
