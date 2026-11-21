"""The ONLY builder of the four ``erp.migration`` consequence kinds (W1). Lines are system-built from re-derived state;
the document identity is a constant uuid5; the posting bridge supplies idempotency, the open-period check and the
atomic create+post. A different request under an identity that is already posted refuses ``version_conflict``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_accounting.models import JournalEntry
from dotmac_kernel.cache import TenantScope
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.holdings.errors import refuse
from erp_v2.holdings.models import OWNER, POSTING_KINDS
from erp_v2.posting.bridge import PostingResult, post_consequence
from erp_v2.posting.rules import PostingRefused

VERSION: Final = "1"


@dataclass(frozen=True, slots=True)
class Line:
    account_ref: str  # an Account UUID
    debit: Decimal
    credit: Decimal
    description: str | None = None
    dimension_refs: tuple = ()


@dataclass(frozen=True, slots=True)
class Source:
    owner: str
    document_kind: str
    document_id: str
    version: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Consequence:
    source: Source
    posting_date: date
    description: str
    currency_code: str
    exchange_rate: Decimal
    entries: tuple[Line, ...]
    fingerprint: str


def build(
    *, kind: str, document_id: str, fingerprint: str, posting_date: date, description: str, currency: str,
    lines: tuple[Line, ...],
) -> Consequence:  # fmt: skip
    if kind not in POSTING_KINDS:
        refuse("posting_kind_unknown", kind)
    source = Source(OWNER, kind, document_id, VERSION, fingerprint)
    return Consequence(source, posting_date, description, currency, Decimal(1), lines, fingerprint)


def existing_journal(db: Session, tenant: UUID, kind: str, document_id: str) -> JournalEntry | None:
    return db.scalar(
        select(JournalEntry).where(
            JournalEntry.tenant_id == tenant,
            JournalEntry.source_owner == OWNER,
            JournalEntry.source_document_kind == kind,
            JournalEntry.source_document_id == document_id,
        )
    )


def post(
    db: Session, tenant: UUID, consequence: Consequence, *, approval_ref: str, actor: str, now: datetime | None = None
) -> PostingResult:
    """Post once. An identity already posted with a DIFFERENT fingerprint refuses ``version_conflict`` (no write)."""
    prior = existing_journal(db, tenant, consequence.source.document_kind, consequence.source.document_id)
    if prior is not None and prior.source_fingerprint != consequence.source.fingerprint:
        refuse("version_conflict", "this identity is already posted from a different plan")
    try:
        return post_consequence(
            db, scope=TenantScope(tenant), consequence=consequence, approval_ref=approval_ref, actor=actor, now=now
        )
    except PostingRefused as exc:
        return refuse(f"posting_refused:{exc.code}", str(exc))


def gross_lines(control: UUID, holding: UUID, positive: Decimal, negative: Decimal, *, label: str) -> tuple[Line, ...]:
    """Holding lines: items with D > 0 (``positive``) credit the control and debit the holding account; D < 0
    (``negative``, as a magnitude) debit the control and credit the holding account. One line per side that has an
    amount (gross, never netted)."""
    lines: list[Line] = []
    if positive:
        lines += [Line(str(holding), positive, Decimal(0), label), Line(str(control), Decimal(0), positive, label)]
    if negative:
        lines += [Line(str(control), negative, Decimal(0), label), Line(str(holding), Decimal(0), negative, label)]
    return tuple(lines)
