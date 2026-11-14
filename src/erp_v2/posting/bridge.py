"""Post a Starter owner module's accounting consequence through the accounting commands.

The caller owns the transaction (kernel request boundary); this function never
commits. Everything it writes happens inside one SAVEPOINT, so any refusal
leaves zero rows even if the caller catches the exception and continues.

Fail-closed: unknown consequence kind, unbalanced or malformed consequence,
account reference in an undeclared form, unknown/inactive/non-postable
account, and missing/closed/locked/adjustment/reopened period all refuse with
``PostingRefused`` before or instead of any journal being created. Replay of an
already-posted consequence returns the existing journal and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import (
    AccountingError,
    AccountKind,
    CreateJournal,
    JournalKind,
    JournalLineInput,
    JournalStatus,
    PeriodStatus,
    SourceIdentity,
)
from dotmac_accounting.models import Account, FiscalPeriod, JournalEntry
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.posting import rules
from erp_v2.posting.rules import ConsequenceLike, PostingRefused, RefPolicy


@dataclass(frozen=True, slots=True)
class PostingResult:
    journal_id: UUID
    journal_number: str
    replayed: bool


def _resolve_accounts(
    db: Session, tenant_id: UUID, policy: RefPolicy, refs: list[str], *, require_open: bool
) -> dict[str, Account]:
    resolved: dict[str, Account] = {}
    for ref in dict.fromkeys(refs):
        form, value = rules.classify_ref(policy, ref)
        column = Account.id == UUID(value) if form == "uuid" else Account.code == value
        rows = list(db.scalars(select(Account).where(Account.tenant_id == tenant_id, column)))
        if len(rows) != 1:
            raise PostingRefused("account_unknown", "account reference does not resolve to exactly one tenant account")
        account = rows[0]
        if require_open and (
            not account.is_active or not account.posting_allowed or account.kind != AccountKind.POSTING
        ):
            raise PostingRefused("account_not_postable", f"account {account.code} is not open for posting")
        resolved[ref] = account
    return resolved


def _require_open_period(db: Session, tenant_id: UUID, posting_date) -> None:
    periods = list(
        db.scalars(
            select(FiscalPeriod).where(
                FiscalPeriod.tenant_id == tenant_id,
                FiscalPeriod.start_date <= posting_date,
                FiscalPeriod.end_date >= posting_date,
            )
        )
    )
    if len(periods) != 1:
        raise PostingRefused("period_missing", "posting date is not in exactly one fiscal period")
    period = periods[0]
    if period.status != PeriodStatus.OPEN or period.is_adjustment:
        raise PostingRefused("period_closed", f"period is {period.status.value.lower()} or adjustment-only")


def post_consequence(
    db: Session,
    *,
    scope: TenantScope,
    consequence: ConsequenceLike,
    approval_ref: str,
    actor: str,
    now: datetime | None = None,
) -> PostingResult:
    """Create and post exactly one balanced journal for ``consequence``, or refuse."""
    policy = rules.validate(consequence)
    if not isinstance(approval_ref, str) or not approval_ref.strip() or len(approval_ref) > 255:
        raise PostingRefused("approval_ref_required")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 255:
        raise PostingRefused("actor_required")
    source = consequence.source
    tenant_id = scope.tenant_id
    moment = now or datetime.now(UTC)
    create_key = rules.idempotency_key(source, "create")
    post_key = rules.idempotency_key(source, "post")
    try:
        with db.begin_nested():
            existing = db.scalar(
                select(JournalEntry).where(
                    JournalEntry.tenant_id == tenant_id,
                    JournalEntry.source_owner == source.owner,
                    JournalEntry.source_document_kind == source.document_kind,
                    JournalEntry.source_document_id == source.document_id,
                    JournalEntry.source_version == source.version,
                )
            )
            if existing is not None and existing.status not in (JournalStatus.POSTED, JournalStatus.DRAFT):
                raise PostingRefused("journal_state_refused", existing.status.value.lower())
            replay = existing is not None and existing.status == JournalStatus.POSTED
            accounts = _resolve_accounts(
                db, tenant_id, policy, [e.account_ref for e in consequence.entries], require_open=not replay
            )
            if not replay:
                _require_open_period(db, tenant_id, consequence.posting_date)
            command = CreateJournal(
                number=rules.journal_number(source),
                kind=JournalKind.STANDARD,
                entry_date=consequence.posting_date,
                posting_date=consequence.posting_date,
                currency_code=consequence.currency_code,
                exchange_rate=consequence.exchange_rate,
                description=consequence.description,
                reference=rules.source_key(source),
                source=SourceIdentity(
                    owner=source.owner,
                    document_kind=source.document_kind,
                    document_id=source.document_id,
                    version=source.version,
                    fingerprint=source.fingerprint,
                ),
                lines=tuple(
                    JournalLineInput(
                        account_id=accounts[e.account_ref].id,
                        debit=Decimal(e.debit),
                        credit=Decimal(e.credit),
                        description=e.description,
                    )
                    for e in consequence.entries
                ),
            )
            # Returns the existing journal on exact replay; a different request
            # under the same source identity raises Conflict (refused below).
            journal = accounting.create_journal(
                db,
                scope=scope,
                command=command,
                idempotency_key=create_key,
                idempotency_expires_at=None,
                recorded_at=moment,
            )
            if journal.status == JournalStatus.POSTED:
                return PostingResult(journal.id, journal.number, True)
            if journal.status != JournalStatus.DRAFT:
                raise PostingRefused("journal_state_refused", journal.status.value.lower())
            posted = accounting.post_journal(
                db,
                scope=scope,
                journal_id=journal.id,
                approval_reference=approval_ref,
                posted_by=actor,
                idempotency_key=post_key,
                idempotency_expires_at=None,
                posted_at=moment,
            )
            return PostingResult(posted.id, posted.number, False)
    except PostingRefused:
        raise
    except (AccountingError, IdempotencyConflict) as exc:
        raise PostingRefused("accounting_refused", str(exc)) from exc
    except IntegrityError as exc:
        raise PostingRefused("accounting_conflict", "journal number or source identity conflict") from exc
