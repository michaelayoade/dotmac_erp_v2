"""The bucketed legacy claim extract (S8 section 1). Read-only; refuses unless the buckets reconcile.

Every legacy claim of the tenant falls in exactly one bucket:

* A / A_partial: APPROVED, net payable known, balance due > 0, claim journal POSTED (-> verifier: admit or refuse);
* U: the same without a POSTED claim journal (lands as an imported claim, no liability, no GL);
* N: APPROVED with no net payable (refused ``claim_net_payable_missing`` when a posted claim journal exists);
* S: settled as recorded (PAID, or APPROVED with balance due <= 0);
* O: any other status (imported as is).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID

import sqlalchemy as sa

from erp_v2.importer.payables_history import SOURCE_SCHEMAS, _equal, _id, _read
from erp_v2.importer.payables_reconciliation import _reflect

CLAIM_TABLE = "expense_claim"
CLAIM_FIELDS = [
    "claim_id",
    "organization_id",
    "employee_id",
    "status",
    "currency_code",
    "net_payable_amount",
    "amount_paid",
    "balance_due",
    "advance_adjusted",
    "journal_entry_id",
    "reimbursement_journal_id",
]
OTHER_STATUSES = frozenset(
    {"DRAFT", "SUBMITTED", "PENDING_APPROVAL", "REJECTED", "CANCELLED", "APPROVAL_WITHDRAWN", "EXPIRED"}
)
BUCKET_A, BUCKET_A_PARTIAL, BUCKET_U, BUCKET_N, BUCKET_S, BUCKET_O = "A", "A_partial", "U", "N", "S", "O"
BUCKETS = (BUCKET_A, BUCKET_A_PARTIAL, BUCKET_U, BUCKET_N, BUCKET_S, BUCKET_O)


class ExtractRefused(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ClaimRow:
    claim_id: str
    employee_id: str | None
    status: str
    currency: str
    net: Decimal | None
    paid: Decimal
    balance: Decimal | None
    advance: Decimal
    journal_id: str | None
    reimbursement_journal_id: str | None


def classify_bucket(
    status: str, net: Decimal | None, paid: Decimal, balance: Decimal | None, journal_posted: bool
) -> str:
    """The pure bucket predicate. ``balance`` is the legacy computed balance_due (net - paid), None when net is."""
    if status in OTHER_STATUSES:
        return BUCKET_O
    if status == "PAID":
        return BUCKET_S  # recorded as paid, whatever the computed balance (the ADR-0016 column artefact)
    if status != "APPROVED":
        raise ExtractRefused("census_partition_incomplete")
    if net is None:
        return BUCKET_N
    owed = balance if balance is not None else net - paid
    if owed <= 0:
        return BUCKET_S
    if not journal_posted:
        return BUCKET_U
    return BUCKET_A_PARTIAL if paid > 0 else BUCKET_A


def _dec(value: object, nullable: bool = False) -> Decimal | None:
    if value is None:
        if nullable:
            return None
        raise ExtractRefused("claim_amount_invalid")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ExtractRefused("claim_amount_invalid") from exc
    if not result.is_finite():
        raise ExtractRefused("claim_amount_invalid")
    return result


def claim_schema(src: sa.Connection) -> str | None:
    """The importer's relation (``expense_claim``) resolved to the ONE schema that holds it (fail closed otherwise)."""
    if src.dialect.name != "postgresql":
        return None
    try:
        return _reflect(src, CLAIM_TABLE, None).schema
    except ValueError as exc:
        raise ExtractRefused("source_table_resolution") from exc


def to_row(raw: dict) -> ClaimRow:
    return ClaimRow(
        claim_id=_id(raw["claim_id"]),
        employee_id=None if raw["employee_id"] is None else _id(raw["employee_id"]),
        status=str(raw["status"]),
        currency=str(raw["currency_code"] or ""),
        net=_dec(raw["net_payable_amount"], True),
        paid=_dec(raw["amount_paid"]) or Decimal(0),
        balance=_dec(raw["balance_due"], True),
        advance=_dec(raw["advance_adjusted"]) or Decimal(0),
        journal_id=None if raw["journal_entry_id"] is None else _id(raw["journal_entry_id"]),
        reimbursement_journal_id=None
        if raw["reimbursement_journal_id"] is None
        else _id(raw["reimbursement_journal_id"]),
    )


def read_claims(src: sa.Connection, tenant: UUID, claim_id: str | None = None) -> list[ClaimRow]:
    schema = claim_schema(src)
    key, value = ("claim_id", claim_id) if claim_id is not None else ("organization_id", tenant)
    rows = _read(src, CLAIM_TABLE, schema=schema, key=key, value=value, fields=CLAIM_FIELDS)
    return [to_row(r) for r in rows if _equal(r["organization_id"], tenant)]


def journal_posted(src: sa.Connection, journal_id: str | None) -> bool:
    if journal_id is None:
        return False
    rows = _read(src, "journal_entry", key="journal_entry_id", value=journal_id)
    return len(rows) == 1 and str(rows[0]["status"]).strip().upper() == "POSTED"


@dataclass(frozen=True)
class Census:
    buckets: dict[str, list[ClaimRow]]
    by_status: dict[str, int]
    n_posted: tuple[str, ...]  # claim ids of bucket N that have a POSTED claim journal

    def totals(self, bucket: str) -> tuple[int, Decimal, int]:
        """(count, Σ owed, distinct employees) of a bucket."""
        rows = self.buckets[bucket]
        return (
            len(rows),
            sum((owed(r) for r in rows), Decimal(0)),
            len({r.employee_id for r in rows if r.employee_id}),
        )


def owed(row: ClaimRow) -> Decimal:
    if row.net is None:
        return Decimal(0)
    return row.balance if row.balance is not None else row.net - row.paid


def census(src: sa.Connection, tenant: UUID) -> Census:
    """Bucket every claim; refuses ``census_partition_incomplete`` unless the buckets sum to the source counts."""
    rows = read_claims(src, tenant)
    buckets: dict[str, list[ClaimRow]] = {b: [] for b in BUCKETS}
    n_posted: list[str] = []
    for row in rows:
        needs_journal = row.status == "APPROVED" and (row.net is None or owed(row) > 0)
        posted = journal_posted(src, row.journal_id) if needs_journal else False
        bucket = classify_bucket(row.status, row.net, row.paid, row.balance, posted)
        buckets[bucket].append(row)
        if bucket == BUCKET_N and posted:
            n_posted.append(row.claim_id)
    # reconcile against an independent per-status count of the source relation
    table = _reflect(src, CLAIM_TABLE, claim_schema(src))
    counted = {
        str(s): int(n)
        for s, n in src.execute(
            sa.select(sa.cast(table.c.status, sa.Text), sa.func.count())
            .where(sa.func.replace(sa.cast(table.c.organization_id, sa.Text), "-", "") == tenant.hex)
            .group_by(table.c.status)
        )
    }
    in_buckets = Counter(r.status for members in buckets.values() for r in members)
    if dict(in_buckets) != counted or sum(len(m) for m in buckets.values()) != len(rows):
        raise ExtractRefused("census_partition_incomplete")
    return Census(buckets, counted, tuple(sorted(n_posted)))


def completeness(c: Census, admitted: tuple[int, Decimal], refused: tuple[int, Decimal]) -> dict:
    """The S8 section 1 identities, per currency (functional only is admitted, so one entry per currency seen).

    ``a_total = admitted + refused`` and ``approved_open = A + A_partial + U``."""
    out: dict[str, dict] = {}
    approved = [r for b in (BUCKET_A, BUCKET_A_PARTIAL, BUCKET_U) for r in c.buckets[b]]
    for ccy in sorted({r.currency for r in approved} | {""}):
        a = [r for b in (BUCKET_A, BUCKET_A_PARTIAL) for r in c.buckets[b] if r.currency == ccy]
        u = [r for r in c.buckets[BUCKET_U] if r.currency == ccy]
        if not a and not u and ccy == "":
            continue
        a_sum, u_sum = sum((owed(r) for r in a), Decimal(0)), sum((owed(r) for r in u), Decimal(0))
        open_rows = [r for r in approved if r.currency == ccy]
        open_sum = sum((owed(r) for r in open_rows), Decimal(0))
        out[ccy or "?"] = dict(
            a_count=len(a),
            a_sum=format(a_sum, "f"),
            u_count=len(u),
            u_sum=format(u_sum, "f"),
            approved_open_count=len(open_rows),
            approved_open_sum=format(open_sum, "f"),
            a_equals_admitted_plus_refused=ccy != "NGN"
            or (len(a) == admitted[0] + refused[0] and a_sum == admitted[1] + refused[1]),
            approved_open_equals_a_plus_u=open_sum == a_sum + u_sum and len(open_rows) == len(a) + len(u),
        )
    return out


__all__ = ["BUCKETS", "SOURCE_SCHEMAS", "Census", "ClaimRow", "ExtractRefused", "census", "classify_bucket"]
