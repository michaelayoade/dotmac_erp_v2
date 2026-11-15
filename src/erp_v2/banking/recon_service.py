"""Bank reconciliation commands and read model (B7).

Scope: match imported bank statement lines (banking a1, imported by B6) to the posted ledger lines on
the bank account's cash GL account (accounting a1 ``posted_ledger_lines``; never raw settlement or
cash observations, REVIEW-A5 A5-6). A reconciliation is per bank account and statement period:
OPEN (workspace) -> PREPARED (snapshot, preparer) -> APPROVED (different person, zero difference);
CANCELLED frees the period, and a prepared reconciliation can be returned to OPEN.

Invariants
* Zero unexplained difference, exact Decimal: statement closing balance == GL balance + sum of
  itemized reconciling items. Every uncleared in-scope statement line and ledger line must be covered
  by exactly one typed item; nothing is rounded or tolerated.
* Reconciling items are typed. GL-side timing items (deposit in transit, outstanding payment) are
  allowed. Bank-side items (unrecorded bank charge / receipt) are recorded so the gap is visible but
  BLOCK preparation: the fix is a B2 manual journal (never auto-posted here), after which the ledger
  line is matched to the statement line.
* No GL writes, no external calls. This module only reads banking and accounting tables and writes
  its own four ``erp_bank_*`` tables (and audit events).
* Preparer != approver, and the approver also differs from the opener and from every person who
  matched or itemized inside the scope (checked here; the database enforces approver != preparer,
  approver != opener, difference = 0 and immutability of approved rows).
* Every command takes the bank account row lock first, then the reconciliation row, so concurrent
  commands on one account serialise; each runs in one SAVEPOINT so a refusal leaves zero rows.
* Matching is idempotent: re-submitting the same member set returns the existing match.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_accounting.contracts import JournalStatus, PeriodStatus
from dotmac_accounting.models import Account, FiscalPeriod, JournalEntry, JournalLine, PostedLedgerLine
from dotmac_banking.models import BankAccount, BankStatement, BankStatementLine
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.models import Party
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.banking import recon_models as m
from erp_v2.banking.recon_models import BankReconciliation, BankReconItem, BankReconMatch, BankReconMatchLine
from erp_v2.journals import models as journal_models
from erp_v2.journals.policy import FunctionalCurrencyError, functional_currency

SOURCE_APPLICATION: Final = "dotmac_erp"
MAX_GROUP: Final = 50
MAX_ITEMS_PER_REQUEST: Final = 100
SUGGEST_WINDOW_DAYS: Final = 7
SUGGEST_BANK_LINES: Final = 100
PAGE_SIZE: Final = 50
MAX_PAGE: Final = 2000
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

AUDIT_OPENED: Final = "erp.bank_recon.opened"
AUDIT_MATCHED: Final = "erp.bank_recon.matched"
AUDIT_UNMATCHED: Final = "erp.bank_recon.unmatched"
AUDIT_ITEM_ADDED: Final = "erp.bank_recon.item_added"
AUDIT_ITEM_WITHDRAWN: Final = "erp.bank_recon.item_withdrawn"
AUDIT_PREPARED: Final = "erp.bank_recon.prepared"
AUDIT_RETURNED: Final = "erp.bank_recon.returned"
AUDIT_APPROVED: Final = "erp.bank_recon.approved"
AUDIT_CANCELLED: Final = "erp.bank_recon.cancelled"
AUDIT_ACTIONS: Final = (
    AUDIT_OPENED,
    AUDIT_MATCHED,
    AUDIT_UNMATCHED,
    AUDIT_ITEM_ADDED,
    AUDIT_ITEM_WITHDRAWN,
    AUDIT_PREPARED,
    AUDIT_RETURNED,
    AUDIT_APPROVED,
    AUDIT_CANCELLED,
)


class ReconRefused(ValueError):
    """Fail-closed refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --- small helpers ---------------------------------------------------------


def _text(value: object, label: str, maximum: int = 500) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ReconRefused("invalid_input", f"{label} is required")
    if len(text) > maximum or _CONTROL.search(text):
        raise ReconRefused("invalid_input", f"{label} is too long or contains control characters")
    return text


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _actor(db: Session, tenant_id: UUID, party_id: UUID) -> UUID:
    found = db.scalar(
        select(Party.id).where(Party.id == party_id, Party.tenant_id == tenant_id, Party.is_active.is_(True))
    )
    if found is None:
        raise ReconRefused("actor_invalid", "the acting party is not an active party of this tenant")
    return found


def _audit(db: Session, *, tenant_id: UUID, actor: UUID, action: str, entity_id: UUID, **details: object) -> None:
    write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_party_id=actor,
        actor_type="user",
        actor_id=str(actor),
        source_application=SOURCE_APPLICATION,
        action=action,
        entity_type="bank_reconciliation",
        entity_id=str(entity_id),
        is_success=True,
        details={
            key: (str(value) if isinstance(value, UUID | date | Decimal) else value) for key, value in details.items()
        },
    )


def bank_effect(direction: str, amount: Decimal) -> Decimal:
    """Effect on the bank balance: a credit to the customer's account increases it."""
    return amount if direction == "credit" else -amount


def gl_effect(debit: Decimal, credit: Decimal) -> Decimal:
    """Effect on the cash GL balance (debit-normal asset)."""
    return Decimal(debit) - Decimal(credit)


def _lock_account(db: Session, tenant_id: UUID, account_id: UUID) -> BankAccount:
    account = db.scalar(
        select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.id == account_id).with_for_update()
    )
    if account is None:
        raise ReconRefused("account_unknown", "bank account not found in this tenant")
    return account


def resolve_gl_account(db: Session, tenant_id: UUID, account: BankAccount) -> UUID:
    """The bank account's cash GL account: a canonical account id or a declared account code."""
    ref = (account.cash_account_ref or "").strip()
    if not ref:
        raise ReconRefused("gl_account_unresolved", "the bank account has no cash account reference")
    try:
        parsed = UUID(ref)
    except ValueError:
        parsed = None
    if parsed is not None and str(parsed) == ref:
        column = Account.id == parsed
    elif parsed is not None:
        raise ReconRefused("gl_account_unresolved", "the cash account reference is not a canonical account id")
    else:
        column = Account.code == ref
    rows = list(db.scalars(select(Account.id).where(Account.tenant_id == tenant_id, column)))
    if len(rows) != 1:
        raise ReconRefused("gl_account_unresolved", "the cash account reference does not resolve to one account")
    _refuse_shared_gl(db, tenant_id, account, rows[0])
    return rows[0]


def _refuse_shared_gl(db: Session, tenant_id: UUID, account: BankAccount, gl_account_id: UUID) -> None:
    """Two bank accounts on one cash GL cannot be reconciled one by one: the GL balance mixes both.

    Refused (fail closed) rather than guessed. Finance splits the shared GL (one cash account per bank
    account) or reconciles the pair outside ERP; no allocation between the accounts is invented here.
    """
    code = db.scalar(select(Account.code).where(Account.tenant_id == tenant_id, Account.id == gl_account_id))
    for other in db.scalars(
        select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.id != account.id)
    ):
        ref = (other.cash_account_ref or "").strip()
        if ref == str(gl_account_id) or ref == code:
            raise ReconRefused(
                "shared_cash_gl",
                f"bank account {other.account_code} shares this cash GL account; reconcile outside ERP or split the GL",
            )


def _require_functional(db: Session, tenant_id: UUID, currency_code: str) -> None:
    try:
        functional = functional_currency(db, tenant_id)
    except FunctionalCurrencyError as exc:
        raise ReconRefused(exc.code, str(exc)) from None
    if functional != currency_code:
        # The ledger carries functional amounts; a foreign-currency bank account would need the
        # original-currency legs. Not supported: refuse rather than compare unlike amounts.
        raise ReconRefused(
            "non_functional_currency", "bank accounts outside the functional currency are not reconcilable here"
        )


# --- scope queries ----------------------------------------------------------


def _cleared(side: str, ref_column, as_of: date, tenant_id: UUID):
    """EXISTS: the line sits in an active match that lies wholly inside the scope date."""
    return exists(
        select(BankReconMatchLine.id)
        .join(
            BankReconMatch,
            and_(
                BankReconMatch.tenant_id == BankReconMatchLine.tenant_id,
                BankReconMatch.id == BankReconMatchLine.match_id,
            ),
        )
        .where(
            BankReconMatchLine.tenant_id == tenant_id,
            BankReconMatchLine.side == side,
            BankReconMatchLine.ref_id == ref_column,
            BankReconMatchLine.active.is_(True),
            BankReconMatch.status == m.MATCH_ACTIVE,
            BankReconMatch.latest_date <= as_of,
        )
    )


@dataclass(frozen=True, slots=True)
class Base:
    """The approved baseline of an account: lines dated on or before ``date`` are out of scope, except the
    ledger lines the baseline carried forward as open timing items."""

    date: date
    carried: frozenset[UUID]
    reconciliation_id: UUID


def approved_baseline(db: Session, tenant_id: UUID, account_id: UUID) -> Base | None:
    row = db.scalar(
        select(BankReconciliation).where(
            BankReconciliation.tenant_id == tenant_id,
            BankReconciliation.account_id == account_id,
            BankReconciliation.kind == m.KIND_BASELINE,
            BankReconciliation.status == m.STATUS_APPROVED,
        )
    )
    if row is None:
        return None
    carried = frozenset(
        db.scalars(
            select(BankReconItem.ref_id).where(
                BankReconItem.tenant_id == tenant_id,
                BankReconItem.reconciliation_id == row.id,
                BankReconItem.side == m.SIDE_GL,
                BankReconItem.withdrawn_at.is_(None),
            )
        )
    )
    return Base(row.period_end, carried, row.id)


def _require_base(db: Session, tenant_id: UUID, recon: BankReconciliation) -> Base:
    base = approved_baseline(db, tenant_id, recon.account_id)
    if base is None:
        raise ReconRefused("baseline_required", "the account needs an approved baseline reconciliation first")
    return base


def _bank_scope(tenant_id: UUID, recon: BankReconciliation, base: Base | None):
    cond = and_(
        BankStatement.tenant_id == tenant_id,
        BankStatement.account_id == recon.account_id,
        BankStatement.period_end <= recon.period_end,
    )
    if base is not None:
        cond = and_(cond, BankStatementLine.transaction_date > base.date)
    return cond


def _gl_scope(tenant_id: UUID, recon: BankReconciliation, base: Base | None):
    cond = and_(
        PostedLedgerLine.tenant_id == tenant_id,
        PostedLedgerLine.account_id == recon.gl_account_id,
        PostedLedgerLine.posting_date <= recon.period_end,
    )
    if base is not None:
        after = PostedLedgerLine.posting_date > base.date
        cond = and_(cond, or_(after, PostedLedgerLine.id.in_(base.carried)) if base.carried else after)
    return cond


def uncleared_bank_lines(
    db: Session, tenant_id: UUID, recon: BankReconciliation, base: Base | None = None
) -> list[tuple[UUID, date, str, Decimal]]:
    """(line id, transaction date, direction, amount) of in-scope lines with no wholly-in-scope match."""
    if recon.kind == m.KIND_BASELINE:
        return []  # a baseline needs no statement-line coverage: history is summarised by its balance
    base = base or _require_base(db, tenant_id, recon)
    rows = db.execute(
        select(
            BankStatementLine.id,
            BankStatementLine.transaction_date,
            BankStatementLine.direction,
            BankStatementLine.amount,
        )
        .join(
            BankStatement,
            and_(
                BankStatement.tenant_id == BankStatementLine.tenant_id,
                BankStatement.id == BankStatementLine.statement_id,
            ),
        )
        .where(
            BankStatementLine.tenant_id == tenant_id,
            _bank_scope(tenant_id, recon, base),
            ~_cleared(m.SIDE_BANK, BankStatementLine.id, recon.period_end, tenant_id),
        )
        .order_by(BankStatementLine.transaction_date, BankStatementLine.id)
    ).all()
    return [(r[0], r[1], r[2], Decimal(r[3])) for r in rows]


def uncleared_gl_lines(
    db: Session, tenant_id: UUID, recon: BankReconciliation, base: Base | None = None
) -> list[tuple[UUID, date, Decimal]]:
    """(ledger line id, posting date, effect) of in-scope cash GL lines with no wholly-in-scope match."""
    if recon.kind == m.KIND_BASELINE:
        return []  # baseline items are validated line by line, never by scanning the whole history
    base = base or _require_base(db, tenant_id, recon)
    rows = db.execute(
        select(PostedLedgerLine.id, PostedLedgerLine.posting_date, PostedLedgerLine.debit, PostedLedgerLine.credit)
        .where(
            _gl_scope(tenant_id, recon, base), ~_cleared(m.SIDE_GL, PostedLedgerLine.id, recon.period_end, tenant_id)
        )
        .order_by(PostedLedgerLine.posting_date, PostedLedgerLine.id)
    ).all()
    return [(r[0], r[1], gl_effect(r[2], r[3])) for r in rows]


def gl_balance(db: Session, tenant_id: UUID, recon: BankReconciliation) -> Decimal:
    """Cumulative cash GL balance to the period end (never scoped by the baseline)."""
    value = db.scalar(
        select(func.coalesce(func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0)).where(
            _gl_scope(tenant_id, recon, None)
        )
    )
    return Decimal(value or 0)


def baseline_gl_page(
    db: Session, tenant_id: UUID, recon: BankReconciliation, page: int
) -> tuple[list[tuple[UUID, date, Decimal]], int]:
    """Cash GL lines dated on or before the baseline date, newest first, one page (for choosing items)."""
    where = _gl_scope(tenant_id, recon, None)
    total = db.scalar(select(func.count()).select_from(PostedLedgerLine).where(where)) or 0
    rows = db.execute(
        select(PostedLedgerLine.id, PostedLedgerLine.posting_date, PostedLedgerLine.debit, PostedLedgerLine.credit)
        .where(where)
        .order_by(PostedLedgerLine.posting_date.desc(), PostedLedgerLine.id)
        .offset((max(1, page) - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    return [(r[0], r[1], gl_effect(r[2], r[3])) for r in rows], int(total)


# --- D2: unrecorded bank items in a closed period -------------------------------


def _first_open_period(db: Session, tenant_id: UUID) -> FiscalPeriod | None:
    return db.scalar(
        select(FiscalPeriod)
        .where(FiscalPeriod.tenant_id == tenant_id, FiscalPeriod.status == PeriodStatus.OPEN)
        .order_by(FiscalPeriod.start_date)
        .limit(1)
    )


def _period_is_open(db: Session, tenant_id: UUID, day: date) -> bool:
    status = db.scalar(
        select(FiscalPeriod.status).where(
            FiscalPeriod.tenant_id == tenant_id, FiscalPeriod.start_date <= day, FiscalPeriod.end_date >= day
        )
    )
    return status == PeriodStatus.OPEN


def _link_valid(
    db: Session, tenant_id: UUID, recon: BankReconciliation, journal_id: UUID, line_date: date, effect: Decimal
) -> bool:
    """The linked B2 manual journal is live, dated in the first open period, and books exactly the bank
    line's effect on this cash GL, so the next reconciliation can match the bank line to its ledger line."""
    if _period_is_open(db, tenant_id, line_date):
        return False
    manual = db.scalar(
        select(journal_models.ManualJournal).where(
            journal_models.ManualJournal.tenant_id == tenant_id, journal_models.ManualJournal.id == journal_id
        )
    )
    if manual is None or manual.state not in (
        journal_models.STATE_PREPARED,
        journal_models.STATE_POST_REQUESTED,
        journal_models.STATE_POSTED,
    ):
        return False
    journal = db.scalar(
        select(JournalEntry).where(JournalEntry.tenant_id == tenant_id, JournalEntry.id == manual.journal_id)
    )
    period = _first_open_period(db, tenant_id)
    if journal is None or period is None or not (period.start_date <= journal.posting_date <= period.end_date):
        return False
    booked = db.scalar(
        select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).where(
            JournalLine.tenant_id == tenant_id,
            JournalLine.journal_id == journal.id,
            JournalLine.account_id == recon.gl_account_id,
        )
    )
    return Decimal(booked or 0) == effect


# --- evaluation (the zero-difference gate) -------------------------------


@dataclass(frozen=True, slots=True)
class Evaluation:
    statement_closing: Decimal
    gl_balance: Decimal
    items_total: Decimal
    difference: Decimal
    uncovered_bank: tuple[UUID, ...]
    uncovered_gl: tuple[UUID, ...]
    stale_items: tuple[UUID, ...]
    unrecorded_items: tuple[UUID, ...]
    cleared_bank_count: int
    cleared_gl_count: int
    snapshot_sha256: str
    issues: tuple[str, ...] = field(default=())

    @property
    def clean(self) -> bool:
        return not self.issues


def _items(db: Session, tenant_id: UUID, reconciliation_id: UUID) -> list[BankReconItem]:
    return list(
        db.scalars(
            select(BankReconItem)
            .where(
                BankReconItem.tenant_id == tenant_id,
                BankReconItem.reconciliation_id == reconciliation_id,
                BankReconItem.withdrawn_at.is_(None),
            )
            .order_by(BankReconItem.side, BankReconItem.ref_id)
        )
    )


def evaluate(db: Session, tenant_id: UUID, recon: BankReconciliation) -> Evaluation:
    statement = db.scalar(
        select(BankStatement).where(BankStatement.tenant_id == tenant_id, BankStatement.id == recon.statement_id)
    )
    if statement is None or statement.closing_balance is None:
        raise ReconRefused("statement_unusable", "the statement has no closing balance")
    closing = Decimal(statement.closing_balance)
    if recon.kind == m.KIND_BASELINE:
        return _evaluate_baseline(db, tenant_id, recon, closing)
    base = _require_base(db, tenant_id, recon)
    bank_open = uncleared_bank_lines(db, tenant_id, recon, base)
    gl_open = uncleared_gl_lines(db, tenant_id, recon, base)
    bank_by_id = {row[0]: row for row in bank_open}
    gl_by_id = {row[0]: row for row in gl_open}
    items = _items(db, tenant_id, recon.id)
    covered = {(i.side, i.ref_id) for i in items}
    uncovered_bank = tuple(row[0] for row in bank_open if (m.SIDE_BANK, row[0]) not in covered)
    uncovered_gl = tuple(row[0] for row in gl_open if (m.SIDE_GL, row[0]) not in covered)
    stale: list[UUID] = []
    unrecorded: list[UUID] = []
    total = Decimal(0)
    for item in items:
        if item.side == m.SIDE_BANK:
            line = bank_by_id.get(item.ref_id)
            expected = None if line is None else bank_effect(line[2], line[3])
        else:
            gl_line = gl_by_id.get(item.ref_id)
            expected = None if gl_line is None else -gl_line[2]
        if expected is None or expected != Decimal(item.signed_amount):
            stale.append(item.id)
            continue
        if item.item_type in m.UNRECORDED_TYPES:
            if item.journal_id is None:
                unrecorded.append(item.id)  # open period: needs a journal first, blocks preparation
            elif not _link_valid(db, tenant_id, recon, item.journal_id, line[1], expected):
                stale.append(item.id)
                continue
        total += Decimal(item.signed_amount)
    balance = gl_balance(db, tenant_id, recon)
    difference = closing - (balance + total)
    cleared_bank = db.scalar(
        select(func.count())
        .select_from(BankStatementLine)
        .join(
            BankStatement,
            and_(
                BankStatement.tenant_id == BankStatementLine.tenant_id,
                BankStatement.id == BankStatementLine.statement_id,
            ),
        )
        .where(
            BankStatementLine.tenant_id == tenant_id,
            _bank_scope(tenant_id, recon, base),
            _cleared(m.SIDE_BANK, BankStatementLine.id, recon.period_end, tenant_id),
        )
    )
    cleared_gl = db.scalar(
        select(func.count())
        .select_from(PostedLedgerLine)
        .where(_gl_scope(tenant_id, recon, base), _cleared(m.SIDE_GL, PostedLedgerLine.id, recon.period_end, tenant_id))
    )
    issues: list[str] = []
    if uncovered_bank:
        issues.append("uncovered_bank_lines")
    if uncovered_gl:
        issues.append("uncovered_gl_lines")
    if stale:
        issues.append("stale_items")
    if unrecorded:
        issues.append("unrecorded_bank_items")
    if difference != 0:
        issues.append("difference_nonzero")
    digest = hashlib.sha256(
        json.dumps(
            {
                "recon": str(recon.id),
                "statement": str(recon.statement_id),
                "as_of": recon.period_end.isoformat(),
                "closing": str(closing),
                "gl": str(balance),
                "items": sorted(
                    [i.side, str(i.ref_id), i.item_type, str(i.signed_amount), str(i.journal_id)] for i in items
                ),
                "cleared": [int(cleared_bank or 0), int(cleared_gl or 0)],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return Evaluation(
        statement_closing=closing,
        gl_balance=balance,
        items_total=total,
        difference=difference,
        uncovered_bank=uncovered_bank,
        uncovered_gl=uncovered_gl,
        stale_items=tuple(stale),
        unrecorded_items=tuple(unrecorded),
        cleared_bank_count=int(cleared_bank or 0),
        cleared_gl_count=int(cleared_gl or 0),
        snapshot_sha256=digest,
        issues=tuple(issues),
    )


def _evaluate_baseline(db: Session, tenant_id: UUID, recon: BankReconciliation, closing: Decimal) -> Evaluation:
    """Opening reconciliation at the baseline date: statement balance == cash GL balance + outstanding timing
    items bound to real ledger lines. No plug, no coverage of history, no statement-side items."""
    items = _items(db, tenant_id, recon.id)
    ledger = {
        row[0]: row
        for row in db.execute(
            select(
                PostedLedgerLine.id, PostedLedgerLine.posting_date, PostedLedgerLine.debit, PostedLedgerLine.credit
            ).where(
                _gl_scope(tenant_id, recon, None),
                PostedLedgerLine.id.in_([i.ref_id for i in items if i.side == m.SIDE_GL]),
            )
        )
    }
    stale: list[UUID] = []
    total = Decimal(0)
    for item in items:
        row = ledger.get(item.ref_id) if item.side == m.SIDE_GL else None
        if row is None or item.item_type not in m.TIMING_TYPES or item.journal_id is not None:
            stale.append(item.id)
            continue
        if -gl_effect(row[2], row[3]) != Decimal(item.signed_amount):
            stale.append(item.id)
            continue
        total += Decimal(item.signed_amount)
    balance = gl_balance(db, tenant_id, recon)
    difference = closing - (balance + total)
    issues = []
    if stale:
        issues.append("stale_items")
    if difference != 0:
        issues.append("difference_nonzero")
    digest = hashlib.sha256(
        json.dumps(
            {
                "recon": str(recon.id),
                "kind": "baseline",
                "statement": str(recon.statement_id),
                "as_of": recon.period_end.isoformat(),
                "closing": str(closing),
                "gl": str(balance),
                "items": sorted([i.side, str(i.ref_id), i.item_type, str(i.signed_amount)] for i in items),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return Evaluation(
        statement_closing=closing,
        gl_balance=balance,
        items_total=total,
        difference=difference,
        uncovered_bank=(),
        uncovered_gl=(),
        stale_items=tuple(stale),
        unrecorded_items=(),
        cleared_bank_count=0,
        cleared_gl_count=0,
        snapshot_sha256=digest,
        issues=tuple(issues),
    )


# --- reconciliation lifecycle ---------------------------------------------


def _recon(db: Session, tenant_id: UUID, reconciliation_id: UUID, *, lock: bool = False) -> BankReconciliation:
    query = select(BankReconciliation).where(
        BankReconciliation.tenant_id == tenant_id, BankReconciliation.id == reconciliation_id
    )
    # populate_existing: a row first read without the lock must be re-read fresh once the lock is held.
    row = db.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if row is None:
        raise ReconRefused("reconciliation_unknown", "reconciliation not found in this tenant")
    return row


def _locked_recon(db: Session, tenant_id: UUID, reconciliation_id: UUID) -> tuple[BankAccount, BankReconciliation]:
    """Account lock first, then the reconciliation row: the one lock order for every command."""
    row = _recon(db, tenant_id, reconciliation_id)
    account = _lock_account(db, tenant_id, row.account_id)
    return account, _recon(db, tenant_id, reconciliation_id, lock=True)


def live_reconciliation_account(db: Session, tenant_id: UUID, reconciliation_id: UUID) -> UUID:
    """The bank account of an OPEN or PREPARED reconciliation of this tenant, else a refusal."""
    row = _recon(db, tenant_id, reconciliation_id)
    if row.status not in (m.STATUS_OPEN, m.STATUS_PREPARED):
        raise ReconRefused("state_refused", "only a live reconciliation can be changed")
    return row.account_id


def _lock_gl_account(db: Session, tenant_id: UUID, recon: BankReconciliation) -> None:
    """Serialise against postings to the cash GL (accounting a1 post_journal locks each line's Account row).

    Global lock order: bank account -> reconciliation -> GL account. Never lock a banking account row
    after posting. Every posting then either committed before this lock or waits until we commit.
    """
    found = db.scalar(
        select(Account.id).where(Account.tenant_id == tenant_id, Account.id == recon.gl_account_id).with_for_update()
    )
    if found is None:
        raise ReconRefused("gl_account_unresolved", "the cash GL account no longer exists")


def open_reconciliation(
    db: Session,
    *,
    tenant_id: UUID,
    actor: UUID,
    account_id: UUID,
    statement_id: UUID,
    baseline_date: date | None = None,
    now: datetime | None = None,
) -> BankReconciliation:
    """Open a periodic reconciliation, or (with ``baseline_date``) the account's one opening baseline.

    The baseline date is declared explicitly (the flip date) and must equal the anchor statement's end date.
    A periodic reconciliation needs an approved baseline and a period end after it.
    """
    moment = _now(now)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            account = _lock_account(db, tenant_id, account_id)
            statement = db.scalar(
                select(BankStatement).where(
                    BankStatement.tenant_id == tenant_id,
                    BankStatement.id == statement_id,
                    BankStatement.account_id == account.id,
                )
            )
            if statement is None:
                raise ReconRefused("statement_unknown", "statement not found for this bank account")
            if statement.closing_balance is None:
                raise ReconRefused("statement_unusable", "the statement has no closing balance to reconcile against")
            if statement.currency_code != account.currency_code or statement.minor_units != account.minor_units:
                raise ReconRefused("statement_unusable", "statement currency does not match the bank account")
            _require_functional(db, tenant_id, account.currency_code)
            gl_account_id = resolve_gl_account(db, tenant_id, account)
            # One in-flight reconciliation per account, and periods only move forward: a new period end
            # must be after the latest approved one (no back-filling once later matches are locked).
            in_flight = db.scalar(
                select(BankReconciliation.id).where(
                    BankReconciliation.tenant_id == tenant_id,
                    BankReconciliation.account_id == account.id,
                    BankReconciliation.status.in_((m.STATUS_OPEN, m.STATUS_PREPARED)),
                )
            )
            if in_flight is not None:
                raise ReconRefused(
                    "reconciliation_exists", "this account already has an open or prepared reconciliation"
                )
            latest = db.scalar(
                select(func.max(BankReconciliation.period_end)).where(
                    BankReconciliation.tenant_id == tenant_id,
                    BankReconciliation.account_id == account.id,
                    BankReconciliation.status == m.STATUS_APPROVED,
                )
            )
            if latest is not None and statement.period_end <= latest:
                raise ReconRefused(
                    "period_not_after_approved", f"the latest approved reconciliation already covers {latest}"
                )
            baseline = approved_baseline(db, tenant_id, account.id)
            if baseline_date is not None:
                if statement.period_end != baseline_date:
                    raise ReconRefused(
                        "baseline_date_mismatch", "the baseline statement must end exactly on the baseline date"
                    )
                exists_already = db.scalar(
                    select(BankReconciliation.id).where(
                        BankReconciliation.tenant_id == tenant_id,
                        BankReconciliation.account_id == account.id,
                        BankReconciliation.kind == m.KIND_BASELINE,
                        BankReconciliation.status.in_(m.LIVE_STATUSES),
                    )
                )
                if exists_already is not None:
                    raise ReconRefused("baseline_exists", "this account already has a baseline reconciliation")
            elif baseline is None:
                raise ReconRefused("baseline_required", "the account needs an approved baseline reconciliation first")
            row = BankReconciliation(
                tenant_id=tenant_id,
                account_id=account.id,
                kind=m.KIND_BASELINE if baseline_date is not None else m.KIND_PERIODIC,
                statement_id=statement.id,
                gl_account_id=gl_account_id,
                period_start=statement.period_start,
                period_end=statement.period_end,
                currency_code=account.currency_code,
                status=m.STATUS_OPEN,
                opened_by_party_id=actor,
                opened_at=moment,
            )
            db.add(row)
            db.flush()
            _audit(db, tenant_id=tenant_id, actor=actor, action=AUDIT_OPENED, entity_id=row.id, account=account.id)
            return row
    except IntegrityError:
        raise ReconRefused("conflict", "a conflicting reconciliation already exists") from None


def prepare(
    db: Session, *, tenant_id: UUID, actor: UUID, reconciliation_id: UUID, now: datetime | None = None
) -> BankReconciliation:
    """OPEN -> PREPARED. Refuses unless the gate is clean: everything covered, difference exactly 0."""
    moment = _now(now)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            _, row = _locked_recon(db, tenant_id, reconciliation_id)
            if row.status != m.STATUS_OPEN:
                raise ReconRefused("state_refused", f"only an open reconciliation can be prepared, not {row.status}")
            _lock_gl_account(db, tenant_id, row)
            result = evaluate(db, tenant_id, row)
            if not result.clean:
                raise ReconRefused("not_reconcilable", ",".join(result.issues))
            row.status = m.STATUS_PREPARED
            row.statement_closing_balance = result.statement_closing
            row.gl_balance = result.gl_balance
            row.items_total = result.items_total
            row.difference = result.difference
            row.snapshot_sha256 = result.snapshot_sha256
            row.prepared_by_party_id = actor
            row.prepared_at = moment
            row.updated_at = moment
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_PREPARED,
                entity_id=row.id,
                snapshot_sha256=result.snapshot_sha256,
                difference=result.difference,
            )
            return row
    except IntegrityError:
        raise ReconRefused("conflict", "the reconciliation could not be prepared") from None


def _participants(db: Session, tenant_id: UUID, recon: BankReconciliation) -> set[UUID]:
    people: set[UUID] = {recon.opened_by_party_id}
    if recon.prepared_by_party_id is not None:
        people.add(recon.prepared_by_party_id)
    for column in (BankReconItem.created_by_party_id, BankReconItem.withdrawn_by_party_id):
        people.update(
            p
            for p in db.scalars(
                select(column).where(BankReconItem.tenant_id == tenant_id, BankReconItem.reconciliation_id == recon.id)
            )
            if p is not None
        )
    # Releasing a match is participation too: whoever unmatched in-scope lines since the last approval.
    last_approved = db.scalar(
        select(func.max(BankReconciliation.approved_at)).where(
            BankReconciliation.tenant_id == tenant_id,
            BankReconciliation.account_id == recon.account_id,
            BankReconciliation.status == m.STATUS_APPROVED,
        )
    )
    released = select(BankReconMatch.unmatched_by_party_id).where(
        BankReconMatch.tenant_id == tenant_id,
        BankReconMatch.account_id == recon.account_id,
        BankReconMatch.status == m.MATCH_UNMATCHED,
        BankReconMatch.latest_date <= recon.period_end,
    )
    if last_approved is not None:
        released = released.where(BankReconMatch.unmatched_at >= last_approved)
    people.update(p for p in db.scalars(released) if p is not None)
    people.update(
        db.scalars(
            select(BankReconMatch.matched_by_party_id).where(
                BankReconMatch.tenant_id == tenant_id,
                BankReconMatch.account_id == recon.account_id,
                BankReconMatch.status == m.MATCH_ACTIVE,
                BankReconMatch.latest_date <= recon.period_end,
                BankReconMatch.locked_by_reconciliation_id.is_(None),
            )
        )
    )
    return people


def approve(
    db: Session, *, tenant_id: UUID, actor: UUID, reconciliation_id: UUID, now: datetime | None = None
) -> BankReconciliation:
    """PREPARED -> APPROVED. Re-evaluates under lock; refuses any drift or non-zero difference."""
    moment = _now(now)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            _, row = _locked_recon(db, tenant_id, reconciliation_id)
            if row.status != m.STATUS_PREPARED:
                raise ReconRefused("state_refused", f"only a prepared reconciliation can be approved, not {row.status}")
            if actor == row.prepared_by_party_id:
                raise ReconRefused("sod_preparer", "the preparer cannot approve their own reconciliation")
            if actor in _participants(db, tenant_id, row):
                raise ReconRefused(
                    "sod_participant", "the approver took part in opening, matching or itemizing this reconciliation"
                )
            _lock_gl_account(db, tenant_id, row)
            result = evaluate(db, tenant_id, row)
            if result.difference != 0:
                raise ReconRefused("difference_nonzero", f"unexplained difference {result.difference}")
            if not result.clean:
                raise ReconRefused("not_reconcilable", ",".join(result.issues))
            if result.snapshot_sha256 != row.snapshot_sha256:
                raise ReconRefused(
                    "snapshot_drift", "the ledger, matches or items changed since preparation; return it"
                )
            row.status = m.STATUS_APPROVED
            row.approved_by_party_id = actor
            row.approved_at = moment
            row.updated_at = moment
            db.flush()
            # Freeze every active match wholly inside the scope: an approved reconciliation is immutable.
            db.execute(
                update(BankReconMatch)
                .where(
                    BankReconMatch.tenant_id == tenant_id,
                    BankReconMatch.account_id == row.account_id,
                    BankReconMatch.status == m.MATCH_ACTIVE,
                    BankReconMatch.latest_date <= row.period_end,
                    BankReconMatch.locked_by_reconciliation_id.is_(None),
                )
                .values(locked_by_reconciliation_id=row.id, updated_at=moment)
            )
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_APPROVED,
                entity_id=row.id,
                snapshot_sha256=row.snapshot_sha256,
            )
            return row
    except IntegrityError:
        raise ReconRefused("conflict", "the reconciliation could not be approved") from None


def return_to_open(
    db: Session, *, tenant_id: UUID, actor: UUID, reconciliation_id: UUID, reason: str, now: datetime | None = None
) -> BankReconciliation:
    """PREPARED -> OPEN, with a mandatory reason (the approver's rejection)."""
    moment = _now(now)
    reason = _text(reason, "reason")
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            _, row = _locked_recon(db, tenant_id, reconciliation_id)
            if row.status != m.STATUS_PREPARED:
                raise ReconRefused("state_refused", f"only a prepared reconciliation can be returned, not {row.status}")
            row.status = m.STATUS_OPEN
            row.statement_closing_balance = None
            row.gl_balance = None
            row.items_total = None
            row.difference = None
            row.snapshot_sha256 = None
            row.prepared_by_party_id = None
            row.prepared_at = None
            row.returned_by_party_id = actor
            row.returned_at = moment
            row.return_reason = reason
            row.updated_at = moment
            db.flush()
            _audit(db, tenant_id=tenant_id, actor=actor, action=AUDIT_RETURNED, entity_id=row.id, reason=reason)
            return row
    except IntegrityError:
        raise ReconRefused("conflict", "the reconciliation could not be returned") from None


def cancel(
    db: Session, *, tenant_id: UUID, actor: UUID, reconciliation_id: UUID, reason: str, now: datetime | None = None
) -> BankReconciliation:
    moment = _now(now)
    reason = _text(reason, "reason")
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            _, row = _locked_recon(db, tenant_id, reconciliation_id)
            if row.status not in (m.STATUS_OPEN, m.STATUS_PREPARED):
                raise ReconRefused("state_refused", f"a {row.status} reconciliation cannot be cancelled")
            row.status = m.STATUS_CANCELLED
            row.cancelled_by_party_id = actor
            row.cancelled_at = moment
            row.cancel_reason = reason
            row.updated_at = moment
            db.flush()
            _audit(db, tenant_id=tenant_id, actor=actor, action=AUDIT_CANCELLED, entity_id=row.id, reason=reason)
            return row
    except IntegrityError:
        raise ReconRefused("conflict", "the reconciliation could not be cancelled") from None


# --- matching ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    match_id: UUID
    replayed: bool


def _member_hash(bank_ids: list[UUID], gl_ids: list[UUID]) -> str:
    members = sorted([f"bank:{i}" for i in bank_ids] + [f"gl:{i}" for i in gl_ids])
    return hashlib.sha256("\n".join(members).encode()).hexdigest()


def _unique_ids(values: object, label: str) -> list[UUID]:
    if not isinstance(values, list | tuple):
        raise ReconRefused("invalid_input", f"{label} must be a list")
    ids = list(dict.fromkeys(values))
    if any(not isinstance(i, UUID) for i in ids):
        raise ReconRefused("invalid_input", f"{label} must be identifiers")
    if len(ids) > MAX_GROUP:
        raise ReconRefused("invalid_input", f"at most {MAX_GROUP} {label} per match")
    return ids


def match(
    db: Session,
    *,
    tenant_id: UUID,
    actor: UUID,
    account_id: UUID,
    bank_line_ids: list[UUID],
    gl_line_ids: list[UUID],
    now: datetime | None = None,
) -> MatchOutcome:
    """Match statement lines to ledger lines with exactly equal net cash effect (idempotent).

    A group with no statement line is allowed only for a reversal pair on the cash GL account
    (original journal and its reversal), which nets to zero by construction.
    """
    moment = _now(now)
    bank_ids = _unique_ids(bank_line_ids, "statement lines")
    gl_ids = _unique_ids(gl_line_ids, "ledger lines")
    if not gl_ids:
        raise ReconRefused("invalid_input", "a match needs at least one ledger line")
    if not bank_ids and len(gl_ids) < 2:
        raise ReconRefused("invalid_input", "a ledger-only match needs a reversal pair")
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            account = _lock_account(db, tenant_id, account_id)
            gl_account_id = resolve_gl_account(db, tenant_id, account)
            member_hash = _member_hash(bank_ids, gl_ids)
            existing = db.scalar(
                select(BankReconMatch).where(
                    BankReconMatch.tenant_id == tenant_id,
                    BankReconMatch.account_id == account.id,
                    BankReconMatch.member_sha256 == member_hash,
                    BankReconMatch.status == m.MATCH_ACTIVE,
                )
            )
            if existing is not None:
                return MatchOutcome(existing.id, replayed=True)
            bank_rows = (
                db.execute(
                    select(
                        BankStatementLine.id,
                        BankStatementLine.transaction_date,
                        BankStatementLine.direction,
                        BankStatementLine.amount,
                    )
                    .join(
                        BankStatement,
                        and_(
                            BankStatement.tenant_id == BankStatementLine.tenant_id,
                            BankStatement.id == BankStatementLine.statement_id,
                        ),
                    )
                    .where(
                        BankStatementLine.tenant_id == tenant_id,
                        BankStatementLine.id.in_(bank_ids),
                        BankStatement.account_id == account.id,
                    )
                ).all()
                if bank_ids
                else []
            )
            gl_rows = db.execute(
                select(
                    PostedLedgerLine.id,
                    PostedLedgerLine.posting_date,
                    PostedLedgerLine.debit,
                    PostedLedgerLine.credit,
                    PostedLedgerLine.journal_id,
                ).where(
                    PostedLedgerLine.tenant_id == tenant_id,
                    PostedLedgerLine.id.in_(gl_ids),
                    PostedLedgerLine.account_id == gl_account_id,
                )
            ).all()
            if len(bank_rows) != len(bank_ids):
                raise ReconRefused("line_unknown", "a statement line does not belong to this bank account")
            if len(gl_rows) != len(gl_ids):
                raise ReconRefused("line_unknown", "a ledger line is not a posted line of this bank account's cash GL")
            base = approved_baseline(db, tenant_id, account.id)
            if base is None:
                raise ReconRefused("baseline_required", "the account needs an approved baseline before matching")
            if any(r[1] <= base.date for r in bank_rows) or any(
                r[1] <= base.date and r[0] not in base.carried for r in gl_rows
            ):
                raise ReconRefused(
                    "before_baseline", "lines dated on or before the baseline are out of scope for matching"
                )
            taken = db.scalar(
                select(func.count())
                .select_from(BankReconMatchLine)
                .where(
                    BankReconMatchLine.tenant_id == tenant_id,
                    BankReconMatchLine.active.is_(True),
                    or_(
                        and_(BankReconMatchLine.side == m.SIDE_BANK, BankReconMatchLine.ref_id.in_(bank_ids)),
                        and_(BankReconMatchLine.side == m.SIDE_GL, BankReconMatchLine.ref_id.in_(gl_ids)),
                    ),
                )
            )
            if taken:
                raise ReconRefused("already_matched", "a line is already part of an active match")
            bank_total = sum((bank_effect(r[2], Decimal(r[3])) for r in bank_rows), Decimal(0))
            gl_total = sum((gl_effect(r[2], r[3]) for r in gl_rows), Decimal(0))
            if bank_total != gl_total:
                raise ReconRefused(
                    "amount_mismatch", f"statement effect {bank_total} differs from ledger effect {gl_total}"
                )
            if not bank_ids:
                _require_reversal_pair(db, tenant_id, {r[4] for r in gl_rows})
            latest = max([r[1] for r in bank_rows] + [r[1] for r in gl_rows])
            row = BankReconMatch(
                tenant_id=tenant_id,
                account_id=account.id,
                status=m.MATCH_ACTIVE,
                member_sha256=member_hash,
                net_amount=bank_total,
                latest_date=latest,
                matched_by_party_id=actor,
                matched_at=moment,
            )
            db.add(row)
            db.flush()
            for r in bank_rows:
                db.add(
                    BankReconMatchLine(
                        tenant_id=tenant_id,
                        match_id=row.id,
                        side=m.SIDE_BANK,
                        ref_id=r[0],
                        amount=bank_effect(r[2], Decimal(r[3])),
                    )
                )
            for r in gl_rows:
                db.add(
                    BankReconMatchLine(
                        tenant_id=tenant_id, match_id=row.id, side=m.SIDE_GL, ref_id=r[0], amount=gl_effect(r[2], r[3])
                    )
                )
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_MATCHED,
                entity_id=row.id,
                bank_lines=len(bank_rows),
                gl_lines=len(gl_rows),
                net_amount=bank_total,
            )
            return MatchOutcome(row.id, replayed=False)
    except IntegrityError:
        raise ReconRefused("already_matched", "a line is already part of an active match") from None


def _require_reversal_pair(db: Session, tenant_id: UUID, journal_ids: set[UUID]) -> None:
    journals = list(
        db.scalars(select(JournalEntry).where(JournalEntry.tenant_id == tenant_id, JournalEntry.id.in_(journal_ids)))
    )
    by_id = {j.id: j for j in journals}
    if len(by_id) != len(journal_ids):
        raise ReconRefused("line_unknown", "a ledger line's journal was not found")
    for journal in journals:
        counterpart = journal.reversal_journal_id or journal.reverses_journal_id
        if counterpart is None or counterpart not in by_id:
            raise ReconRefused("not_a_reversal_pair", "a ledger-only match must contain whole reversal pairs")
        other = by_id[counterpart]
        if not (
            (journal.reversal_journal_id == other.id and other.reverses_journal_id == journal.id)
            or (journal.reverses_journal_id == other.id and other.reversal_journal_id == journal.id)
        ):
            raise ReconRefused("not_a_reversal_pair", "a ledger-only match must contain whole reversal pairs")
        if JournalStatus.VOID in (journal.status, other.status):
            raise ReconRefused("not_a_reversal_pair", "a void journal cannot be matched")


def unmatch(
    db: Session, *, tenant_id: UUID, actor: UUID, match_id: UUID, reason: str, now: datetime | None = None
) -> MatchOutcome:
    """Release a match. Refused once an approved reconciliation covers it. Idempotent."""
    moment = _now(now)
    reason = _text(reason, "reason")
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            peek = db.scalar(
                select(BankReconMatch).where(BankReconMatch.tenant_id == tenant_id, BankReconMatch.id == match_id)
            )
            if peek is None:
                raise ReconRefused("match_unknown", "match not found in this tenant")
            _lock_account(db, tenant_id, peek.account_id)
            row = db.scalar(
                select(BankReconMatch)
                .where(BankReconMatch.tenant_id == tenant_id, BankReconMatch.id == match_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise ReconRefused("match_unknown", "match not found in this tenant")
            if row.status == m.MATCH_UNMATCHED:
                return MatchOutcome(row.id, replayed=True)
            if row.locked_by_reconciliation_id is not None:
                raise ReconRefused("match_locked", "an approved reconciliation covers this match")
            row.status = m.MATCH_UNMATCHED
            row.unmatched_by_party_id = actor
            row.unmatched_at = moment
            row.unmatch_reason = reason
            row.updated_at = moment
            db.flush()
            db.execute(
                update(BankReconMatchLine)
                .where(BankReconMatchLine.tenant_id == tenant_id, BankReconMatchLine.match_id == row.id)
                .values(active=False)
            )
            _audit(db, tenant_id=tenant_id, actor=actor, action=AUDIT_UNMATCHED, entity_id=row.id, reason=reason)
            return MatchOutcome(row.id, replayed=False)
    except IntegrityError:
        raise ReconRefused("conflict", "the match could not be released") from None


# --- reconciling items -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemRequest:
    side: str
    ref_id: UUID
    item_type: str
    note: str
    journal_id: UUID | None = None


def add_items(
    db: Session,
    *,
    tenant_id: UUID,
    actor: UUID,
    reconciliation_id: UUID,
    requests: list[ItemRequest],
    now: datetime | None = None,
) -> list[BankReconItem]:
    """Explain uncleared lines with typed items (all or nothing). Never posts to the ledger."""
    _now(now)
    if not requests or len(requests) > MAX_ITEMS_PER_REQUEST:
        raise ReconRefused("invalid_input", f"between 1 and {MAX_ITEMS_PER_REQUEST} items per request")
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            _, row = _locked_recon(db, tenant_id, reconciliation_id)
            if row.status != m.STATUS_OPEN:
                raise ReconRefused("state_refused", "items can change only while the reconciliation is open")
            baseline_kind = row.kind == m.KIND_BASELINE
            bank_open = {r[0]: r for r in uncleared_bank_lines(db, tenant_id, row)}
            gl_open = {r[0]: r for r in uncleared_gl_lines(db, tenant_id, row)}
            existing = {(i.side, i.ref_id): i for i in _items(db, tenant_id, row.id)}
            created: list[BankReconItem] = []
            for request in requests:
                note = _text(request.note, "note")
                if request.item_type not in m.ITEM_TYPES:
                    raise ReconRefused("item_type_unknown", "unknown reconciling item type")
                if baseline_kind and request.side == m.SIDE_BANK:
                    raise ReconRefused("baseline_gl_items_only", "a baseline carries only ledger-side timing items")
                if request.side == m.SIDE_BANK:
                    line = bank_open.get(request.ref_id)
                    if line is None:
                        raise ReconRefused("line_not_open", "the statement line is not an uncleared line of this scope")
                    amount = bank_effect(line[2], line[3])
                    wanted = m.UNRECORDED_TYPES
                elif request.side == m.SIDE_GL:
                    gl_line = (
                        _baseline_line(db, tenant_id, row, request.ref_id)
                        if baseline_kind
                        else gl_open.get(request.ref_id)
                    )
                    if gl_line is None:
                        raise ReconRefused("line_not_open", "the ledger line is not an uncleared line of this scope")
                    amount = -gl_line[2]
                    wanted = m.TIMING_TYPES
                else:
                    raise ReconRefused("invalid_input", "unknown item side")
                if request.item_type not in wanted:
                    raise ReconRefused("item_type_side", "this item type does not apply to that side")
                # A deposit in transit is a ledger debit; an outstanding payment a ledger credit;
                # an unrecorded bank charge a statement debit; an unrecorded receipt a credit.
                negative = request.item_type in (m.ITEM_DEPOSIT_IN_TRANSIT, m.ITEM_UNRECORDED_BANK_CHARGE)
                if (amount < 0) != negative or amount == 0:
                    raise ReconRefused("item_type_direction", "the item type does not match the line's direction")
                link: UUID | None = None
                if request.item_type in m.UNRECORDED_TYPES:
                    closed = not _period_is_open(db, tenant_id, line[1])
                    if request.journal_id is not None and not closed:
                        raise ReconRefused(
                            "journal_link_not_allowed",
                            "the period is open: post a journal in it, then match; no link is allowed",
                        )
                    if closed:
                        if request.journal_id is None:
                            raise ReconRefused(
                                "journal_link_required",
                                "the period is closed: link a manual journal dated in the first open period",
                            )
                        if not _link_valid(db, tenant_id, row, request.journal_id, line[1], amount):
                            raise ReconRefused(
                                "journal_link_invalid",
                                "the linked journal is not live, not in the first open period, or does not book "
                                "exactly the bank line's effect on this cash GL",
                            )
                        link = request.journal_id
                elif request.journal_id is not None:
                    raise ReconRefused("journal_link_not_allowed", "only unrecorded bank items link a journal")
                prior = existing.get((request.side, request.ref_id))
                if prior is not None:
                    if prior.item_type != request.item_type:
                        raise ReconRefused("item_exists", "the line already carries a different reconciling item")
                    continue  # idempotent replay
                item = BankReconItem(
                    tenant_id=tenant_id,
                    reconciliation_id=row.id,
                    side=request.side,
                    ref_id=request.ref_id,
                    item_type=request.item_type,
                    signed_amount=amount,
                    note=note,
                    journal_id=link,
                    created_by_party_id=actor,
                )
                db.add(item)
                existing[(request.side, request.ref_id)] = item
                created.append(item)
            db.flush()
            if created:
                _audit(
                    db,
                    tenant_id=tenant_id,
                    actor=actor,
                    action=AUDIT_ITEM_ADDED,
                    entity_id=row.id,
                    count=len(created),
                    types=sorted({i.item_type for i in created}),
                    lines=sorted(f"{i.side}:{i.ref_id}" for i in created),
                )
            return created
    except IntegrityError:
        raise ReconRefused("conflict", "a conflicting reconciling item already exists") from None


def _baseline_line(
    db: Session, tenant_id: UUID, recon: BankReconciliation, line_id: UUID
) -> tuple[UUID, date, Decimal] | None:
    r = db.execute(
        select(
            PostedLedgerLine.id, PostedLedgerLine.posting_date, PostedLedgerLine.debit, PostedLedgerLine.credit
        ).where(_gl_scope(tenant_id, recon, None), PostedLedgerLine.id == line_id)
    ).first()
    return None if r is None else (r[0], r[1], gl_effect(r[2], r[3]))


def withdraw_item(
    db: Session,
    *,
    tenant_id: UUID,
    actor: UUID,
    reconciliation_id: UUID,
    item_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Governed withdrawal (who/when/reason, audited); items are never deleted."""
    moment = _now(now)
    reason = _text(reason, "reason")
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            _, row = _locked_recon(db, tenant_id, reconciliation_id)
            if row.status != m.STATUS_OPEN:
                raise ReconRefused("state_refused", "items can change only while the reconciliation is open")
            item = db.scalar(
                select(BankReconItem).where(
                    BankReconItem.tenant_id == tenant_id,
                    BankReconItem.reconciliation_id == row.id,
                    BankReconItem.id == item_id,
                    BankReconItem.withdrawn_at.is_(None),
                )
            )
            if item is None:
                return False
            item.withdrawn_by_party_id = actor
            item.withdrawn_at = moment
            item.withdraw_reason = reason
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_ITEM_WITHDRAWN,
                entity_id=row.id,
                item_type=item.item_type,
                side=item.side,
                ref_id=item.ref_id,
                reason=reason,
            )
            return True
    except IntegrityError:
        raise ReconRefused("conflict", "the item could not be withdrawn") from None


# --- suggestions (read-only) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suggestion:
    bank_line_id: UUID
    gl_line_id: UUID
    score: int
    day_gap: int
    reference_hit: bool


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def suggestions(
    db: Session, tenant_id: UUID, recon: BankReconciliation, *, limit: int = SUGGEST_BANK_LINES
) -> list[Suggestion]:
    """Exact-amount, nearby-date candidates for uncleared statement lines. Nothing is accepted here."""
    open_bank = uncleared_bank_lines(db, tenant_id, recon)[:limit]
    if not open_bank:
        return []
    refs = {
        row.id: row
        for row in db.execute(
            select(BankStatementLine.id, BankStatementLine.reference, BankStatementLine.external_ref).where(
                BankStatementLine.tenant_id == tenant_id, BankStatementLine.id.in_([r[0] for r in open_bank])
            )
        )
    }
    open_gl = uncleared_gl_lines(db, tenant_id, recon)
    by_effect: dict[Decimal, list[tuple[UUID, date]]] = {}
    for gl_id, gl_date, effect in open_gl:
        by_effect.setdefault(effect, []).append((gl_id, gl_date))
    texts = {
        row[0]: (row[1], row[2])
        for row in db.execute(
            select(PostedLedgerLine.id, PostedLedgerLine.description, JournalEntry.reference)
            .join(JournalEntry, JournalEntry.id == PostedLedgerLine.journal_id)
            .where(PostedLedgerLine.tenant_id == tenant_id, PostedLedgerLine.id.in_([g[0] for g in open_gl][:5000]))
        )
    }
    result: list[Suggestion] = []
    for line_id, line_date, direction, amount in open_bank:
        wanted = bank_effect(direction, amount)
        ref = _norm(refs[line_id].reference) or _norm(refs[line_id].external_ref)
        candidates = []
        for gl_id, gl_date in by_effect.get(wanted, []):
            gap = abs((gl_date - line_date).days)
            if gap > SUGGEST_WINDOW_DAYS:
                continue
            description, journal_ref = texts.get(gl_id, (None, None))
            hit = bool(ref) and len(ref) >= 4 and (ref in _norm(description) or ref in _norm(journal_ref))
            candidates.append(Suggestion(line_id, gl_id, 100 - gap * 5 + (30 if hit else 0), gap, hit))
        candidates.sort(key=lambda s: (-s.score, str(s.gl_line_id)))
        result.extend(candidates[:3])
    return result
