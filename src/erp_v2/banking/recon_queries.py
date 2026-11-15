"""Read models for the reconciliation pages. Tenant-filtered, bounded, no writes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from dotmac_accounting.models import JournalEntry, PostedLedgerLine
from dotmac_banking.models import BankAccount, BankStatement, BankStatementLine
from dotmac_kernel.models import Party
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from erp_v2.banking import recon_models as m
from erp_v2.banking import recon_service as svc
from erp_v2.banking.recon_models import BankReconciliation, BankReconItem, BankReconMatch, BankReconMatchLine

LIST_LIMIT = 100
MATCH_LIMIT = 50


def _names(db: Session, tenant_id: UUID, ids: set[UUID | None]) -> dict[UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {
        pid: name
        for pid, name in db.execute(
            select(Party.id, Party.display_name).where(Party.tenant_id == tenant_id, Party.id.in_(wanted))
        )
    }


def reconciliations(db: Session, *, tenant_id: UUID, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    query = (
        select(BankReconciliation, BankAccount)
        .join(
            BankAccount,
            and_(
                BankAccount.tenant_id == BankReconciliation.tenant_id, BankAccount.id == BankReconciliation.account_id
            ),
        )
        .where(BankReconciliation.tenant_id == tenant_id)
        .order_by(BankReconciliation.period_end.desc(), BankReconciliation.opened_at.desc())
        .limit(LIST_LIMIT)
    )
    if statuses:
        query = query.where(BankReconciliation.status.in_(statuses))
    return [{"recon": r, "account": a} for r, a in db.execute(query)]


def openable_statements(db: Session, *, tenant_id: UUID) -> list[dict[str, Any]]:
    """Statements with a closing balance and no live reconciliation, newest first."""
    live = select(BankReconciliation.statement_id).where(
        BankReconciliation.tenant_id == tenant_id, BankReconciliation.status.in_(m.LIVE_STATUSES)
    )
    rows = db.execute(
        select(BankStatement, BankAccount)
        .join(
            BankAccount,
            and_(BankAccount.tenant_id == BankStatement.tenant_id, BankAccount.id == BankStatement.account_id),
        )
        .where(
            BankStatement.tenant_id == tenant_id,
            BankStatement.closing_balance.is_not(None),
            BankStatement.id.not_in(live),
        )
        .order_by(BankStatement.period_end.desc())
        .limit(LIST_LIMIT)
    )
    return [{"statement": s, "account": a} for s, a in rows]


def statement_account(db: Session, *, tenant_id: UUID, statement_id: UUID) -> UUID | None:
    return db.scalar(
        select(BankStatement.account_id).where(BankStatement.tenant_id == tenant_id, BankStatement.id == statement_id)
    )


def _page(page: int) -> tuple[int, int]:
    page = max(1, min(page, svc.MAX_PAGE))
    return page, (page - 1) * svc.PAGE_SIZE


def workspace(
    db: Session, *, tenant_id: UUID, reconciliation_id: UUID, bank_page: int = 1, gl_page: int = 1
) -> dict[str, Any] | None:
    recon = db.scalar(
        select(BankReconciliation).where(
            BankReconciliation.tenant_id == tenant_id, BankReconciliation.id == reconciliation_id
        )
    )
    if recon is None:
        return None
    account = db.scalar(
        select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.id == recon.account_id)
    )
    statement = db.scalar(
        select(BankStatement).where(BankStatement.tenant_id == tenant_id, BankStatement.id == recon.statement_id)
    )
    live = recon.status in (m.STATUS_OPEN, m.STATUS_PREPARED)
    evaluation = svc.evaluate(db, tenant_id, recon) if live else None
    items = list(
        db.scalars(
            select(BankReconItem)
            .where(BankReconItem.tenant_id == tenant_id, BankReconItem.reconciliation_id == recon.id)
            .order_by(BankReconItem.item_type, BankReconItem.created_at)
        )
    )
    item_refs = {(i.side, i.ref_id) for i in items}
    bank_page, bank_offset = _page(bank_page)
    gl_page, gl_offset = _page(gl_page)
    bank_open: list[tuple[UUID, date, str, Decimal]] = []
    gl_open: list[tuple[UUID, date, Decimal]] = []
    bank_total = gl_total = 0
    if live:
        bank_all = svc.uncleared_bank_lines(db, tenant_id, recon)
        gl_all = svc.uncleared_gl_lines(db, tenant_id, recon)
        bank_total, gl_total = len(bank_all), len(gl_all)
        bank_open = bank_all[bank_offset : bank_offset + svc.PAGE_SIZE]
        gl_open = gl_all[gl_offset : gl_offset + svc.PAGE_SIZE]
    bank_text = {
        row[0]: row
        for row in db.execute(
            select(
                BankStatementLine.id,
                BankStatementLine.description,
                BankStatementLine.reference,
                BankStatementLine.counterparty,
            ).where(
                BankStatementLine.tenant_id == tenant_id,
                BankStatementLine.id.in_(
                    [r[0] for r in bank_open] + [i.ref_id for i in items if i.side == m.SIDE_BANK]
                ),
            )
        )
    }
    gl_text = {
        row[0]: row
        for row in db.execute(
            select(
                PostedLedgerLine.id,
                PostedLedgerLine.journal_number,
                PostedLedgerLine.description,
                JournalEntry.reference,
            )
            .join(JournalEntry, JournalEntry.id == PostedLedgerLine.journal_id)
            .where(
                PostedLedgerLine.tenant_id == tenant_id,
                PostedLedgerLine.id.in_([r[0] for r in gl_open] + [i.ref_id for i in items if i.side == m.SIDE_GL]),
            )
        )
    }
    matches = list(
        db.scalars(
            select(BankReconMatch)
            .where(
                BankReconMatch.tenant_id == tenant_id,
                BankReconMatch.account_id == recon.account_id,
                BankReconMatch.status == m.MATCH_ACTIVE,
                BankReconMatch.latest_date <= recon.period_end,
            )
            .order_by(BankReconMatch.matched_at.desc())
            .limit(MATCH_LIMIT)
        )
    )
    members: dict[UUID, list[BankReconMatchLine]] = {}
    if matches:
        for line in db.scalars(
            select(BankReconMatchLine).where(
                BankReconMatchLine.tenant_id == tenant_id,
                BankReconMatchLine.match_id.in_([x.id for x in matches]),
                BankReconMatchLine.active.is_(True),
            )
        ):
            members.setdefault(line.match_id, []).append(line)
    previous = db.execute(
        select(BankReconciliation.period_end, BankReconciliation.approved_at, BankStatement.closing_balance)
        .join(
            BankStatement,
            and_(
                BankStatement.tenant_id == BankReconciliation.tenant_id,
                BankStatement.id == BankReconciliation.statement_id,
            ),
        )
        .where(
            BankReconciliation.tenant_id == tenant_id,
            BankReconciliation.account_id == recon.account_id,
            BankReconciliation.status == m.STATUS_APPROVED,
            BankReconciliation.period_end < recon.period_end,
        )
        .order_by(BankReconciliation.period_end.desc())
        .limit(1)
    ).first()
    continuity_warning = None
    late_postings = 0
    if previous is not None:
        if (
            statement is not None
            and statement.opening_balance is not None
            and previous[2] is not None
            and Decimal(statement.opening_balance) != Decimal(previous[2])
        ):
            continuity_warning = (
                f"statement opening balance {statement.opening_balance} differs from the previous approved "
                f"closing balance {previous[2]}"
            )
        if live:
            late_postings = db.scalar(
                select(func.count())
                .select_from(PostedLedgerLine)
                .where(
                    PostedLedgerLine.tenant_id == tenant_id,
                    PostedLedgerLine.account_id == recon.gl_account_id,
                    PostedLedgerLine.posting_date <= previous[0],
                    PostedLedgerLine.posted_at > previous[1],
                )
            )
    suggested = svc.suggestions(db, tenant_id, recon) if recon.status == m.STATUS_OPEN else []
    people = _names(
        db,
        tenant_id,
        {
            recon.opened_by_party_id,
            recon.prepared_by_party_id,
            recon.approved_by_party_id,
            recon.returned_by_party_id,
            recon.cancelled_by_party_id,
            *(x.matched_by_party_id for x in matches),
        },
    )
    return {
        "recon": recon,
        "account": account,
        "statement": statement,
        "evaluation": evaluation,
        "items": items,
        "item_refs": item_refs,
        "bank_open": bank_open,
        "gl_open": gl_open,
        "bank_total": bank_total,
        "gl_total": gl_total,
        "bank_page": bank_page,
        "gl_page": gl_page,
        "page_size": svc.PAGE_SIZE,
        "bank_text": bank_text,
        "gl_text": gl_text,
        "matches": matches,
        "members": members,
        "suggestions": suggested,
        "people": people,
        "continuity_warning": continuity_warning,
        "late_postings": late_postings or 0,
        "item_types": m.ITEM_TYPES,
        "timing_types": m.TIMING_TYPES,
        "unrecorded_types": m.UNRECORDED_TYPES,
    }
