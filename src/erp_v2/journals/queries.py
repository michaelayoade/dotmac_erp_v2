"""Read models for the journal and period screens. Tenant-filtered; bounded; no writes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from dotmac_accounting.contracts import AccountKind, JournalStatus
from dotmac_accounting.models import Account, FiscalPeriod, JournalEntry, JournalLine
from dotmac_kernel.models import Party
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp_v2.journals import models as m
from erp_v2.journals.models import ManualJournal
from erp_v2.journals.service import MANUAL_OWNER

PAGE_SIZE = 50
MAX_PAGE = 1000
REGISTER_LIMIT = 200


def posting_accounts(db: Session, *, tenant_id: UUID, limit: int = 500) -> list[Account]:
    return list(
        db.scalars(
            select(Account)
            .where(
                Account.tenant_id == tenant_id,
                Account.is_active.is_(True),
                Account.posting_allowed.is_(True),
                Account.kind == AccountKind.POSTING,
            )
            .order_by(Account.code)
            .limit(limit)
        )
    )


def _names(db: Session, tenant_id: UUID, rows: list[ManualJournal]) -> dict[UUID, str]:
    ids: set[UUID] = set()
    for row in rows:
        for value in (
            row.prepared_by_party_id,
            row.requested_by_party_id,
            row.approved_by_party_id,
            row.rejected_by_party_id,
            row.reversal_requested_by_party_id,
            row.reversal_approved_by_party_id,
        ):
            if value is not None:
                ids.add(value)
    if not ids:
        return {}
    return {
        pid: name or email or str(pid)
        for pid, name, email in db.execute(
            select(Party.id, Party.display_name, Party.email).where(Party.tenant_id == tenant_id, Party.id.in_(ids))
        )
    }


def register(db: Session, *, tenant_id: UUID, states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    stmt = (
        select(ManualJournal, JournalEntry)
        .join(
            JournalEntry,
            (JournalEntry.tenant_id == ManualJournal.tenant_id) & (JournalEntry.id == ManualJournal.journal_id),
        )
        .where(ManualJournal.tenant_id == tenant_id)
        .order_by(ManualJournal.created_at.desc())
        .limit(REGISTER_LIMIT)
    )
    if states:
        stmt = stmt.where(ManualJournal.state.in_(states))
    return [{"manual": manual, "journal": journal} for manual, journal in db.execute(stmt)]


def approval_queue(db: Session, *, tenant_id: UUID) -> list[dict[str, Any]]:
    return register(db, tenant_id=tenant_id, states=(m.STATE_POST_REQUESTED, m.STATE_REVERSAL_REQUESTED))


def detail(db: Session, *, tenant_id: UUID, manual_id: UUID) -> dict[str, Any] | None:
    manual = db.scalar(select(ManualJournal).where(ManualJournal.tenant_id == tenant_id, ManualJournal.id == manual_id))
    if manual is None:
        return None
    journal = db.scalar(
        select(JournalEntry).where(JournalEntry.tenant_id == tenant_id, JournalEntry.id == manual.journal_id)
    )
    if journal is None:
        return None
    return {
        "manual": manual,
        "journal": journal,
        "lines": _lines(db, tenant_id, journal.id),
        "names": _names(db, tenant_id, [manual]),
    }


def _lines(db: Session, tenant_id: UUID, journal_id: UUID) -> list[tuple[JournalLine, Account]]:
    return [
        (line, account)
        for line, account in db.execute(
            select(JournalLine, Account)
            .join(Account, (Account.tenant_id == JournalLine.tenant_id) & (Account.id == JournalLine.account_id))
            .where(JournalLine.tenant_id == tenant_id, JournalLine.journal_id == journal_id)
            .order_by(JournalLine.line_number)
            .limit(500)
        )
    ]


def _held_filter(tenant_id: UUID):
    return (
        JournalEntry.tenant_id == tenant_id,
        JournalEntry.status == JournalStatus.DRAFT,
        JournalEntry.source_owner != MANUAL_OWNER,
        ~select(ManualJournal.id)
        .where(ManualJournal.tenant_id == JournalEntry.tenant_id, ManualJournal.journal_id == JournalEntry.id)
        .exists(),
    )


def held_journals(db: Session, *, tenant_id: UUID, page: int) -> dict[str, Any]:
    """One page of held legacy DRAFT journals with no workflow yet. Read-only; no bulk action."""
    page = max(1, min(page, MAX_PAGE))
    total = db.scalar(select(func.count()).select_from(JournalEntry).where(*_held_filter(tenant_id))) or 0
    rows = list(
        db.scalars(
            select(JournalEntry)
            .where(*_held_filter(tenant_id))
            .order_by(JournalEntry.posting_date, JournalEntry.number)
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        )
    )
    return {"journals": rows, "page": page, "total": total, "has_more": page * PAGE_SIZE < total, "max_page": MAX_PAGE}


def held_detail(db: Session, *, tenant_id: UUID, journal_id: UUID) -> dict[str, Any] | None:
    journal = db.scalar(select(JournalEntry).where(JournalEntry.tenant_id == tenant_id, JournalEntry.id == journal_id))
    if journal is None:
        return None
    workflow = db.scalar(
        select(ManualJournal).where(ManualJournal.tenant_id == tenant_id, ManualJournal.journal_id == journal_id)
    )
    return {"journal": journal, "lines": _lines(db, tenant_id, journal.id), "workflow": workflow}


def periods(db: Session, *, tenant_id: UUID) -> list[FiscalPeriod]:
    return list(
        db.scalars(
            select(FiscalPeriod)
            .where(FiscalPeriod.tenant_id == tenant_id)
            .order_by(FiscalPeriod.start_date.desc(), FiscalPeriod.period_number)
            .limit(200)
        )
    )
