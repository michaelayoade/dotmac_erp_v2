"""Manual-journal commands: prepare, request post, approve+post, reject, reverse; period commands.

Accounting a1 stays the authority: every journal is created, posted and reversed
through ``create_journal`` / ``post_journal`` / ``reverse_journal`` (and periods through
``open_period`` / ``soft_close_period`` / ``lock_period``), so balance, active-account,
period and ledger rules are accounting's own. This module adds only what accounting does
not record: who prepared, requested and approved (maker-checker, preparer != approver,
enforced here and by CHECK constraints), a mandatory evidence reference, and audit events.

The caller owns the transaction (kernel request boundary); nothing here commits. Each
command runs inside one SAVEPOINT, so a refusal leaves zero rows even if the caller
catches it and continues. Approvals are recorded in ``erp_manual_journals`` (the approvals
module is not consulted: no tenant approval policy is published for this workflow).

Legacy DRAFT-held journals have no preparer. They enter the same approve+post path one at
a time through ``request_legacy_post``, which needs a Finance review reference; there is
no bulk command. Their source identity stays on the journal; the workflow row snapshots it.

No account, fiscal year, fiscal period or dimension is created here (accounting a1's
create_* helpers carry the known _flush_unique defect).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid5

from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import (
    AccountingError,
    CloseCheck,
    CreateJournal,
    JournalKind,
    JournalLineInput,
    JournalStatus,
    PeriodCloseEvidence,
    PeriodStatus,
    ReverseJournal,
    SourceIdentity,
)
from dotmac_accounting.models import FiscalPeriod, JournalEntry, JournalLine, PostedLedgerLine
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.exceptions import BadRequestError
from dotmac_kernel.idempotency import IdempotencyConflict
from dotmac_kernel.models import Party
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.governance import policy as governance
from erp_v2.journals import models as m
from erp_v2.journals.models import ManualJournal
from erp_v2.journals.policy import FunctionalCurrencyError, functional_currency
from erp_v2.ledger_roles import (
    AP_CONTROL_CODE,
    AR_CONTROL_CODE,
    EMPLOYEE_CLAIMS_CODE,
    LEDGER_FAULT_CONTROL_CODES,
    PAYROLL_PAYABLE_CODE,
    STAFF_LOAN_CODE,
    account_id_for_code,
    control_codes_among,
)
from erp_v2.migration_ledger import rounding_verify
from erp_v2.migration_ledger.common import LedgerRefused

MANUAL_OWNER: Final = "erp.manual"
MANUAL_KIND: Final = "manual_journal"
# The one declared legacy import owner (the import run's source_owner constant). Only its
# DRAFT journals may enter the held-journal path; another module's draft is never ours to post.
LEGACY_OWNER: Final = "legacy-erp"
SOURCE_APPLICATION: Final = "dotmac_erp"
# The one system-built GL-only correction of a proven legacy posting fault (section 3c). Its source
# identity is the only thing the control-account guard admits control lines for, and only after the
# fault is re-verified.
LEDGER_FAULT_KIND: Final = "ledger_fault_correction"
_LEDGER_FAULT_NAMESPACE: Final = UUID("5f0b6d2e-7c1a-4b43-9d58-3a9c2e61f0a7")
# The second GL-only correction kind (B-9, ruling v1640): the per-period rounding-residue clearing on 2000. Its source
# identity is ``<bridge sha256>:<YYYY-MM>`` and the guard re-verifies its amount against the latest bridge version.
ROUNDING_KIND: Final = rounding_verify.ROUNDING_KIND
_ROUNDING_NAMESPACE: Final = UUID("c2a9e1d4-3b58-4f70-8a16-5d7e0b4c9f23")
CONTROL_ROUTES: Final = {
    AP_CONTROL_CODE: "a supplier invoice (adjustment invoice) or supplier credit note",
    PAYROLL_PAYABLE_CODE: "a payroll run, an adjustment component in the next run, or a payout return",
    STAFF_LOAN_CODE: "a staff-loan entry or payroll internal recovery",
    AR_CONTROL_CODE: "a billing document (invoice, credit note or receipt) or a governed holding command",
    EMPLOYEE_CLAIMS_CODE: "an expense claim or a governed holding command",
}
CONTROL_ROUTE_DEFAULT: Final = "the owning subledger document (billing receipt or allocation) or a governed holding command"
ALLOWED_KINDS: Final = (JournalKind.STANDARD, JournalKind.ADJUSTMENT)
MAX_LINES: Final = 200
QUANTUM: Final = Decimal("0.000001")
MAXIMUM: Final = Decimal("99999999999999.999999")

AUDIT_PREPARED: Final = "erp.journal.prepared"
AUDIT_POST_REQUESTED: Final = "erp.journal.post_requested"
AUDIT_POSTED: Final = "erp.journal.posted"
AUDIT_REJECTED: Final = "erp.journal.rejected"
AUDIT_REVERSAL_REQUESTED: Final = "erp.journal.reversal_requested"
AUDIT_REVERSED: Final = "erp.journal.reversed"
AUDIT_REVERSAL_REJECTED: Final = "erp.journal.reversal_rejected"
AUDIT_PERIOD_OPENED: Final = "erp.period.opened"
AUDIT_PERIOD_SOFT_CLOSED: Final = "erp.period.soft_closed"
AUDIT_PERIOD_LOCKED: Final = "erp.period.locked"
AUDIT_ACTIONS: Final = (
    AUDIT_PREPARED,
    AUDIT_POST_REQUESTED,
    AUDIT_POSTED,
    AUDIT_REJECTED,
    AUDIT_REVERSAL_REQUESTED,
    AUDIT_REVERSED,
    AUDIT_REVERSAL_REJECTED,
    AUDIT_PERIOD_OPENED,
    AUDIT_PERIOD_SOFT_CLOSED,
    AUDIT_PERIOD_LOCKED,
)

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class JournalRefused(ValueError):
    """Fail-closed refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class LineInput:
    account_id: UUID
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)
    description: str | None = None


@dataclass(frozen=True, slots=True)
class PrepareJournal:
    request_id: UUID
    posting_date: date
    kind: JournalKind
    description: str
    evidence_ref: str
    lines: tuple[LineInput, ...]


@dataclass(frozen=True, slots=True)
class LedgerFaultCorrection:
    """Finance's choices for a system-built correction; amounts and sides are never typed."""

    source_journal_id: UUID
    control_code: str
    offset_account_id: UUID
    posting_date: date
    evidence_ref: str
    description: str


@dataclass(frozen=True, slots=True)
class RoundingCorrection:
    """The Finance-declared inputs of a rounding correction; the amount and sides come from the bridge."""

    bridge_sha256: str
    period: str  # YYYY-MM of the invoices carrying the T-ROUND-2DP residue
    rounding_account_code: str | None
    evidence_ref: str
    description: str


@dataclass(frozen=True, slots=True)
class Outcome:
    manual_id: UUID
    journal_id: UUID
    state: str
    replayed: bool = False


# --- helpers ---------------------------------------------------------------


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise JournalRefused("invalid_input", f"{label} is required")
    text = value.strip()
    if not text:
        raise JournalRefused("invalid_input", f"{label} is required")
    if len(text) > maximum or _CONTROL.search(text):
        raise JournalRefused("invalid_input", f"{label} is too long or contains control characters")
    return text


def _amount(value: object, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value > MAXIMUM:
        raise JournalRefused("invalid_amount", f"{label} must be a finite non-negative amount")
    if value != value.quantize(QUANTUM):
        raise JournalRefused("invalid_amount", f"{label} has more than six decimals")
    return value


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _scope(tenant_id: UUID) -> TenantScope:
    return TenantScope(tenant_id)


def _actor(db: Session, tenant_id: UUID, party_id: UUID) -> UUID:
    found = db.scalar(
        select(Party.id).where(Party.id == party_id, Party.tenant_id == tenant_id, Party.is_active.is_(True))
    )
    if found is None:
        raise JournalRefused("actor_invalid", "the acting party is not an active party of this tenant")
    return found


def _audit(db: Session, *, tenant_id: UUID, actor: UUID, action: str, entity_type: str, entity_id: str, **details):
    write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_party_id=actor,
        actor_type="user",
        actor_id=str(actor),
        source_application=SOURCE_APPLICATION,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        is_success=True,
        details={key: (str(value) if isinstance(value, UUID | date) else value) for key, value in details.items()},
    )


def _map(exc: Exception) -> JournalRefused:
    if isinstance(exc, JournalRefused):
        return exc
    if isinstance(exc, FunctionalCurrencyError):
        return JournalRefused(exc.code, str(exc))
    if isinstance(exc, IntegrityError):
        return JournalRefused("conflict", "a conflicting record already exists")
    return JournalRefused("accounting_refused", str(exc))


_REFUSABLE = (AccountingError, IdempotencyConflict, BadRequestError, ValueError, IntegrityError)


def _periods_for(db: Session, tenant_id: UUID, day: date) -> list[FiscalPeriod]:
    return list(
        db.scalars(
            select(FiscalPeriod).where(
                FiscalPeriod.tenant_id == tenant_id, FiscalPeriod.start_date <= day, FiscalPeriod.end_date >= day
            )
        )
    )


def _require_open_period(db: Session, tenant_id: UUID, day: date, kind: JournalKind) -> FiscalPeriod:
    """Only an OPEN period accepts a manual posting (REOPENED is never offered)."""
    periods = _periods_for(db, tenant_id, day)
    if len(periods) != 1:
        raise JournalRefused("period_missing", "the date is not in exactly one fiscal period")
    period = periods[0]
    if period.status != PeriodStatus.OPEN:
        raise JournalRefused("period_closed", f"the fiscal period is {period.status.value.lower()}")
    if period.is_adjustment and kind not in (JournalKind.ADJUSTMENT, JournalKind.CLOSING):
        raise JournalRefused("period_adjustment_only", "the period accepts adjustment journals only")
    return period


def _lock_row(db: Session, tenant_id: UUID, manual_id: UUID) -> ManualJournal:
    row = db.scalar(
        select(ManualJournal)
        .where(ManualJournal.tenant_id == tenant_id, ManualJournal.id == manual_id)
        .with_for_update()
    )
    if row is None:
        raise JournalRefused("not_found", "manual journal was not found")
    return row


def _journal(db: Session, tenant_id: UUID, journal_id: UUID) -> JournalEntry:
    journal = db.scalar(select(JournalEntry).where(JournalEntry.tenant_id == tenant_id, JournalEntry.id == journal_id))
    if journal is None:
        raise JournalRefused("not_found", "journal was not found")
    return journal


def _fingerprint(payload: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _outcome(row: ManualJournal, *, replayed: bool = False) -> Outcome:
    return Outcome(row.id, row.journal_id, row.state, replayed)


# --- control-account guard (V2) --------------------------------------------


def _journal_account_ids(db: Session, tenant_id: UUID, journal_id: UUID) -> set[UUID]:
    return set(
        db.scalars(
            select(JournalLine.account_id).where(
                JournalLine.tenant_id == tenant_id, JournalLine.journal_id == journal_id
            )
        )
    )


def _refuse_control(codes: set[str]) -> None:
    code = sorted(codes)[0]
    raise JournalRefused(
        "control_account_requires_subledger_document",
        f"{', '.join(sorted(codes))} is a control account and is never posted bare; "
        f"correct it through {CONTROL_ROUTES.get(code, CONTROL_ROUTE_DEFAULT)}",
    )


def _control_net(db: Session, tenant_id: UUID, journal_id: UUID, account_id: UUID) -> Decimal:
    return db.scalar(
        select(func.coalesce(func.sum(JournalLine.debit_functional - JournalLine.credit_functional), 0)).where(
            JournalLine.tenant_id == tenant_id,
            JournalLine.journal_id == journal_id,
            JournalLine.account_id == account_id,
        )
    )


def _fault_key(source_journal_id: UUID, control_code: str) -> str:
    return f"{source_journal_id}:{control_code}"


def _source_ledger_delta(db: Session, tenant_id: UUID, source_journal_id: UUID, account_id: UUID) -> Decimal:
    """Σ(ledger debit - credit) minus Σ(journal line debit_functional - credit_functional), one journal and account.

    Both sides are in the functional currency: the ledger line amounts are functional, and the journal
    line's own debit/credit are in the document currency, so a clean journal at another rate must not
    read as a fault.
    """
    ledger = db.scalar(
        select(func.coalesce(func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0)).where(
            PostedLedgerLine.tenant_id == tenant_id,
            PostedLedgerLine.journal_id == source_journal_id,
            PostedLedgerLine.account_id == account_id,
        )
    )
    return Decimal(ledger) - Decimal(_control_net(db, tenant_id, source_journal_id, account_id))


def _other_posted_corrections(db: Session, tenant_id: UUID, key: str, exclude: UUID | None) -> list[UUID]:
    query = select(JournalEntry.id).where(
        JournalEntry.tenant_id == tenant_id,
        JournalEntry.source_owner == MANUAL_OWNER,
        JournalEntry.source_document_kind == LEDGER_FAULT_KIND,
        JournalEntry.source_document_id == key,
        JournalEntry.status == JournalStatus.POSTED,
    )
    if exclude is not None:
        query = query.where(JournalEntry.id != exclude)
    return list(db.scalars(query))


def _residual_fault(db: Session, tenant_id: UUID, source_journal_id: UUID, code: str, account_id: UUID) -> Decimal:
    """The source journal's ledger-vs-line delta net of POSTED corrections: 0 once corrected."""
    residual = _source_ledger_delta(db, tenant_id, source_journal_id, account_id)
    for posted_id in _other_posted_corrections(db, tenant_id, _fault_key(source_journal_id, code), None):
        residual += Decimal(_control_net(db, tenant_id, posted_id, account_id))
    return residual


def _verify_ledger_fault(db: Session, tenant_id: UUID, journal: JournalEntry) -> None:
    """Re-prove that this system-built correction still corrects exactly the proven fault.

    This re-verification must stay UNCONDITIONAL: it is the only thing that makes the GL-only door safe,
    so no approval policy (including the coming optional dual-control setting) may switch it off.
    """
    try:
        source_text, control_code = (journal.source_document_id or "").rsplit(":", 1)
        source_id = UUID(source_text)
    except ValueError as exc:
        raise JournalRefused("ledger_fault_source_invalid", "the correction's source identity is malformed") from exc
    control_id = account_id_for_code(db, tenant_id, control_code) if control_code in LEDGER_FAULT_CONTROL_CODES else None
    if control_id is None:
        raise JournalRefused("ledger_fault_source_invalid", "the correction's control account is not valid")
    source = db.scalar(select(JournalEntry).where(JournalEntry.tenant_id == tenant_id, JournalEntry.id == source_id))
    if source is None or source.source_owner != LEGACY_OWNER or source.status != JournalStatus.POSTED:
        raise JournalRefused(
            "ledger_fault_source_invalid", "the faulty source journal is no longer a posted legacy journal"
        )
    if _other_posted_corrections(db, tenant_id, _fault_key(source_id, control_code), journal.id):
        raise JournalRefused("ledger_fault_changed", "another correction for this fault has been posted")
    correction = Decimal(_control_net(db, tenant_id, journal.id, control_id))
    if _source_ledger_delta(db, tenant_id, source_id, control_id) + correction != 0:
        raise JournalRefused("ledger_fault_changed", "the ledger fault no longer matches this correction")


def _is_ledger_fault_journal(journal: JournalEntry) -> bool:
    return journal.source_owner == MANUAL_OWNER and journal.source_document_kind == LEDGER_FAULT_KIND


def _is_rounding_journal(journal: JournalEntry) -> bool:
    return journal.source_owner == MANUAL_OWNER and journal.source_document_kind == ROUNDING_KIND


def _verify_rounding(db: Session, tenant_id: UUID, journal: JournalEntry) -> None:
    """Unconditional, like the ledger-fault re-verification: no policy may switch it off."""
    try:
        rounding_verify.verify(db, tenant_id, journal)
    except LedgerRefused as exc:
        raise JournalRefused(exc.code, exc.detail) from exc


def _holding_guard(db: Session, tenant_id: UUID, account_ids: set[UUID]) -> None:
    """B-14 tightening: a holding account (one that carries an ``erp_migration_holdings`` row, or that a flip run
    resolved as a holding account) moves only through the governed holding commands, never through a manual journal or a
    held draft. A bare line would silently break the holding tie. No window, no setting and no override flag."""
    if not account_ids:
        return
    from erp_v2.holdings.models import Holding, HoldingAccount

    hit = db.scalar(
        select(Holding.id).where(Holding.tenant_id == tenant_id, Holding.holding_account_id.in_(account_ids)).limit(1)
    ) or db.scalar(  # a configured holding account is guarded even before it carries a row (L6)
        select(HoldingAccount.id)
        .where(HoldingAccount.tenant_id == tenant_id, HoldingAccount.holding_account_id.in_(account_ids))
        .limit(1)
    )
    if hit is not None:
        raise JournalRefused(
            "holding_account_requires_holding_command",
            "a migration holding account moves only through holding release or writeoff",
        )


def _control_guard(
    db: Session,
    tenant_id: UUID,
    *,
    journal: JournalEntry | None = None,
    account_ids: set[UUID] | None = None,
    reversal: bool = False,
) -> None:
    """Refuse any control-account line (2000/2040/1410/1400/2045/prepayment) not carried by its subledger document.

    A typed line set (prepare) is never admitted: the only door is the system-built ledger-fault
    correction, recognised by its source identity and re-verified here (not on a reversal, which
    simply re-creates the original fault). No window, no setting and no override flag.
    """
    ids = set(account_ids or ()) if journal is None else _journal_account_ids(db, tenant_id, journal.id)
    _holding_guard(db, tenant_id, ids)
    codes = control_codes_among(db, tenant_id, ids)
    if not codes:
        return
    if journal is not None and _is_ledger_fault_journal(journal):
        if not reversal:
            _verify_ledger_fault(db, tenant_id, journal)
        return
    if journal is not None and _is_rounding_journal(journal):
        if not reversal:
            _verify_rounding(db, tenant_id, journal)
        return
    _refuse_control(codes)


# --- prepare ---------------------------------------------------------------


def prepare_journal(
    db: Session, *, tenant_id: UUID, actor: UUID, command: PrepareJournal, now: datetime | None = None
) -> Outcome:
    """Create a DRAFT manual journal in the tenant functional currency (preparer step)."""
    moment = _now(now)
    evidence = _text(command.evidence_ref, "evidence reference", 255)
    description = _text(command.description, "description", 2000)
    if command.kind not in ALLOWED_KINDS:
        raise JournalRefused("invalid_input", "journal kind must be STANDARD or ADJUSTMENT")
    if not 2 <= len(command.lines) <= MAX_LINES:
        raise JournalRefused("invalid_input", f"a journal needs between 2 and {MAX_LINES} lines")
    debit = credit = Decimal(0)
    lines: list[JournalLineInput] = []
    for number, line in enumerate(command.lines, start=1):
        amount_debit = _amount(line.debit, f"line {number} debit")
        amount_credit = _amount(line.credit, f"line {number} credit")
        if (amount_debit > 0) == (amount_credit > 0):
            raise JournalRefused("invalid_amount", f"line {number} must carry an amount on exactly one side")
        line_text = None if line.description is None else _text(line.description, f"line {number} description", 2000)
        debit += amount_debit
        credit += amount_credit
        lines.append(JournalLineInput(line.account_id, amount_debit, amount_credit, line_text))
    if debit != credit or debit <= 0:
        raise JournalRefused("unbalanced", "total debit must equal total credit and be positive")
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            currency = functional_currency(db, tenant_id)
            _require_open_period(db, tenant_id, command.posting_date, command.kind)
            _control_guard(db, tenant_id, account_ids={line.account_id for line in lines})
            fingerprint = _fingerprint(
                {
                    "request_id": str(command.request_id),
                    "kind": command.kind.value,
                    "posting_date": command.posting_date.isoformat(),
                    "description": description,
                    "evidence_ref": evidence,
                    "currency": currency,
                    "lines": [[str(x.account_id), str(x.debit), str(x.credit), x.description] for x in lines],
                }
            )
            journal = accounting.create_journal(
                db,
                scope=_scope(tenant_id),
                command=CreateJournal(
                    number="MJ-" + command.request_id.hex[:24].upper(),
                    kind=command.kind,
                    entry_date=command.posting_date,
                    posting_date=command.posting_date,
                    currency_code=currency,
                    exchange_rate=Decimal(1),
                    description=description,
                    reference=evidence,
                    source=SourceIdentity(
                        owner=MANUAL_OWNER,
                        document_kind=MANUAL_KIND,
                        document_id=str(command.request_id),
                        version="1",
                        fingerprint=fingerprint,
                    ),
                    lines=tuple(lines),
                ),
                idempotency_key=f"erp.manual:{command.request_id}:create",
                idempotency_expires_at=None,
                recorded_at=moment,
            )
            existing = db.scalar(
                select(ManualJournal).where(
                    ManualJournal.tenant_id == tenant_id, ManualJournal.journal_id == journal.id
                )
            )
            if existing is not None:
                return _outcome(existing, replayed=True)
            if journal.status != JournalStatus.DRAFT:
                raise JournalRefused("journal_state_refused", journal.status.value.lower())
            row = ManualJournal(
                tenant_id=tenant_id,
                dual_control=governance.dual_control(db, tenant_id),
                journal_id=journal.id,
                origin=m.ORIGIN_MANUAL,
                state=m.STATE_PREPARED,
                evidence_ref=evidence,
                prepared_by_party_id=actor,
                prepared_at=moment,
            )
            db.add(row)
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_PREPARED,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=journal.id,
                number=journal.number,
                evidence_ref=evidence,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


# --- ledger-fault correction (V2, section 3c) -------------------------------


def prepare_ledger_fault_correction(
    db: Session, *, tenant_id: UUID, actor: UUID, command: LedgerFaultCorrection, now: datetime | None = None
) -> Outcome:
    """Prepare the GL-only reversal of a PROVEN erroneous posting on a control account (preparer step).

    The source must be a POSTED legacy-erp journal whose ledger lines on the control account differ
    from its journal lines by a non-zero delta. The lines are built here (the delta on the control
    account, the opposite side on Finance's offset account), never typed. The correction exists at
    most once per (source journal, control code): a repeat replays. It then follows the normal
    request / approve / post path with its preparer != approver rule; the control guard re-verifies
    the fault at request and at approval.
    """
    moment = _now(now)
    evidence = _text(command.evidence_ref, "evidence reference", 255)
    description = _text(command.description, "description", 2000)
    if command.control_code not in LEDGER_FAULT_CONTROL_CODES:
        raise JournalRefused("invalid_input", "the control account must be 2000, 2040 or 1410")
    key = _fault_key(command.source_journal_id, command.control_code)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            source = db.scalar(
                select(JournalEntry)
                .where(JournalEntry.tenant_id == tenant_id, JournalEntry.id == command.source_journal_id)
                .with_for_update()
            )
            if source is None or source.source_owner != LEGACY_OWNER or source.status != JournalStatus.POSTED:
                raise JournalRefused(
                    "ledger_fault_source_invalid", "the source must be a posted journal from the legacy import"
                )
            control_id = account_id_for_code(db, tenant_id, command.control_code)
            if control_id is None:
                raise JournalRefused("invalid_input", "the control account does not exist for this tenant")
            offset = control_codes_among(db, tenant_id, {command.offset_account_id})
            if offset or command.offset_account_id == control_id:
                raise JournalRefused("offset_is_control_account", "the offset must not be a control account")
            history = db.execute(
                select(ManualJournal, JournalEntry)
                .join(
                    JournalEntry,
                    (JournalEntry.tenant_id == ManualJournal.tenant_id) & (JournalEntry.id == ManualJournal.journal_id),
                )
                .where(
                    ManualJournal.tenant_id == tenant_id,
                    JournalEntry.source_owner == MANUAL_OWNER,
                    JournalEntry.source_document_kind == LEDGER_FAULT_KIND,
                    JournalEntry.source_document_id == key,
                )
            ).all()
            # A REJECTED or REVERSED correction is finished: a new version may be prepared. One that is
            # still live replays only for the same full fingerprint; any difference is a named refusal, never a replay.
            live = [(row, entry) for row, entry in history if row.state not in (m.STATE_REJECTED, m.STATE_REVERSED)]
            if live:
                row, entry = live[0]
                used = db.scalar(
                    select(JournalLine.account_id).where(
                        JournalLine.tenant_id == tenant_id,
                        JournalLine.journal_id == entry.id,
                        JournalLine.account_id != control_id,
                    )
                )
                # Replay only for the FULL fingerprint: offset, posting date, evidence reference and amount.
                same = (
                    used == command.offset_account_id
                    and entry.posting_date == command.posting_date
                    and entry.reference == evidence
                    and abs(Decimal(_control_net(db, tenant_id, entry.id, control_id)))
                    == abs(_source_ledger_delta(db, tenant_id, source.id, control_id))
                )
                if not same:
                    raise JournalRefused(
                        "ledger_fault_correction_active",
                        "a correction for this fault is already in progress with a different offset, date, "
                        "evidence reference or amount; reject or reverse it before preparing another",
                    )
                if row.state in (m.STATE_PREPARED, m.STATE_POST_REQUESTED):
                    _verify_ledger_fault(db, tenant_id, entry)
                return _outcome(row, replayed=True)
            version = str(max((int(entry.source_version) for _row, entry in history), default=0) + 1)
            request_id = uuid5(_LEDGER_FAULT_NAMESPACE, f"{key}:{version}")
            delta = _residual_fault(db, tenant_id, source.id, command.control_code, control_id)
            if delta == 0:
                raise JournalRefused("no_ledger_fault", "the ledger agrees with the journal lines on this account")
            amount = abs(delta)
            # delta < 0: the ledger holds extra credit, so debit the control account (and the reverse).
            control_debit = delta < 0
            lines = (
                JournalLineInput(
                    control_id, amount if control_debit else Decimal(0), Decimal(0) if control_debit else amount, None
                ),
                JournalLineInput(
                    command.offset_account_id,
                    Decimal(0) if control_debit else amount,
                    amount if control_debit else Decimal(0),
                    None,
                ),
            )
            currency = functional_currency(db, tenant_id)
            _require_open_period(db, tenant_id, command.posting_date, JournalKind.STANDARD)
            fingerprint = _fingerprint(
                {
                    "key": key,
                    "version": version,
                    "posting_date": command.posting_date.isoformat(),
                    "description": description,
                    "evidence_ref": evidence,
                    "currency": currency,
                    "lines": [[str(x.account_id), str(x.debit), str(x.credit)] for x in lines],
                }
            )
            journal = accounting.create_journal(
                db,
                scope=_scope(tenant_id),
                command=CreateJournal(
                    number="LF-" + request_id.hex[:24].upper(),
                    kind=JournalKind.STANDARD,
                    entry_date=command.posting_date,
                    posting_date=command.posting_date,
                    currency_code=currency,
                    exchange_rate=Decimal(1),
                    description=description,
                    reference=evidence,
                    source=SourceIdentity(
                        owner=MANUAL_OWNER,
                        document_kind=LEDGER_FAULT_KIND,
                        document_id=key,
                        version=version,
                        fingerprint=fingerprint,
                    ),
                    lines=lines,
                ),
                idempotency_key=f"erp.manual:{request_id}:create",
                idempotency_expires_at=None,
                recorded_at=moment,
            )
            if journal.status != JournalStatus.DRAFT:
                raise JournalRefused("journal_state_refused", journal.status.value.lower())
            row = ManualJournal(
                tenant_id=tenant_id,
                dual_control=governance.dual_control(db, tenant_id),
                journal_id=journal.id,
                origin=m.ORIGIN_MANUAL,
                state=m.STATE_PREPARED,
                evidence_ref=evidence,
                prepared_by_party_id=actor,
                prepared_at=moment,
            )
            db.add(row)
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_PREPARED,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=journal.id,
                number=journal.number,
                evidence_ref=evidence,
                ledger_fault_source=source.id,
                control_code=command.control_code,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


# --- rounding-residue correction (B-9, ruling v1640) -------------------------


def _rounding_posting_date(db: Session, tenant_id: UUID, period: str) -> date:
    """The invoices' period if it is open, otherwise the first open period (the start date of either)."""
    year, month = int(period[:4]), int(period[5:])
    in_period = _periods_for(db, tenant_id, date(year, month, 1))
    if len(in_period) == 1 and in_period[0].status == PeriodStatus.OPEN and not in_period[0].is_adjustment:
        return in_period[0].start_date
    first_open = db.scalar(
        select(FiscalPeriod)
        .where(
            FiscalPeriod.tenant_id == tenant_id,
            FiscalPeriod.status == PeriodStatus.OPEN,
            FiscalPeriod.is_adjustment.is_(False),
        )
        .order_by(FiscalPeriod.start_date)
        .limit(1)
    )
    if first_open is None:
        raise JournalRefused("period_closed", "there is no open period for the rounding correction")
    return first_open.start_date


def prepare_rounding_correction(
    db: Session, *, tenant_id: UUID, actor: UUID, command: RoundingCorrection, now: datetime | None = None
) -> Outcome:
    """Prepare the ONE system-built Dr/Cr 2000 correction of a period's T-ROUND-2DP residue (preparer step).

    The amount and sides are derived from the latest sha-bound bridge version (never typed); the rounding account
    is a required declared input. At most one live correction exists per (bridge sha, period): a repeat replays when
    identical, anything else is a named refusal. It then follows the normal request / approve / post path; the
    control guard re-verifies the amount against the bridge at request and at approval.
    """
    moment = _now(now)
    evidence = _text(command.evidence_ref, "evidence reference", 255)
    description = _text(command.description, "description", 2000)
    if not command.rounding_account_code or not command.rounding_account_code.strip():
        raise JournalRefused("rounding_account_required", "Finance must name the rounding account")
    try:
        key = rounding_verify.key_for(command.bridge_sha256, command.period)
    except LedgerRefused as exc:
        raise JournalRefused(exc.code, exc.detail) from exc
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            try:
                body = rounding_verify.load_bridge(db, tenant_id, command.bridge_sha256)
            except LedgerRefused as exc:
                raise JournalRefused(exc.code, exc.detail) from exc
            if body.get("rounding_account_code") != command.rounding_account_code:
                raise JournalRefused("rounding_account_mismatch", "the declared account is not the bridge's")
            rounding_id = account_id_for_code(db, tenant_id, command.rounding_account_code)
            ap_id = account_id_for_code(db, tenant_id, AP_CONTROL_CODE)
            if rounding_id is None or ap_id is None:
                raise JournalRefused("rounding_account_required", "the rounding account does not exist")
            if rounding_id == ap_id or control_codes_among(db, tenant_id, {rounding_id}):
                raise JournalRefused("offset_is_control_account", "the rounding account must not be a control account")
            history = db.execute(
                select(ManualJournal, JournalEntry)
                .join(
                    JournalEntry,
                    (JournalEntry.tenant_id == ManualJournal.tenant_id) & (JournalEntry.id == ManualJournal.journal_id),
                )
                .where(
                    ManualJournal.tenant_id == tenant_id,
                    JournalEntry.source_owner == MANUAL_OWNER,
                    JournalEntry.source_document_kind == ROUNDING_KIND,
                    JournalEntry.source_document_id == key,
                )
            ).all()
            live = [(row, entry) for row, entry in history if row.state not in (m.STATE_REJECTED, m.STATE_REVERSED)]
            amount_due = rounding_verify.due(db, tenant_id, body, command.period)
            if live:
                row, entry = live[0]
                if row.state in (m.STATE_PREPARED, m.STATE_POST_REQUESTED):
                    _verify_rounding(db, tenant_id, entry)
                if entry.reference != evidence:
                    raise JournalRefused(
                        "rounding_correction_active",
                        "a correction for this bridge and period exists with a different evidence reference",
                    )
                return _outcome(row, replayed=True)
            if amount_due == 0:
                raise JournalRefused("rounding_nothing_due", "no residue is outstanding for this period")
            posting_date = _rounding_posting_date(db, tenant_id, command.period)
            _require_open_period(db, tenant_id, posting_date, JournalKind.STANDARD)
            version = str(max((int(entry.source_version) for _row, entry in history), default=0) + 1)
            request_id = uuid5(_ROUNDING_NAMESPACE, f"{key}:{version}")
            debit = amount_due > 0  # amount_due is the 2000 net (Dr - Cr) the correction must carry
            amount = abs(amount_due)
            lines = (
                JournalLineInput(ap_id, amount if debit else Decimal(0), Decimal(0) if debit else amount, None),
                JournalLineInput(rounding_id, Decimal(0) if debit else amount, amount if debit else Decimal(0), None),
            )
            currency = functional_currency(db, tenant_id)
            fingerprint = _fingerprint(
                {
                    "key": key,
                    "version": version,
                    "posting_date": posting_date.isoformat(),
                    "evidence_ref": evidence,
                    "currency": currency,
                    "lines": [[str(x.account_id), str(x.debit), str(x.credit)] for x in lines],
                }
            )
            journal = accounting.create_journal(
                db,
                scope=_scope(tenant_id),
                command=CreateJournal(
                    number="RR-" + request_id.hex[:24].upper(),
                    kind=JournalKind.STANDARD,
                    entry_date=posting_date,
                    posting_date=posting_date,
                    currency_code=currency,
                    exchange_rate=Decimal(1),
                    description=description,
                    reference=evidence,
                    source=SourceIdentity(
                        owner=MANUAL_OWNER,
                        document_kind=ROUNDING_KIND,
                        document_id=key,
                        version=version,
                        fingerprint=fingerprint,
                    ),
                    lines=lines,
                ),
                idempotency_key=f"erp.manual:{request_id}:create",
                idempotency_expires_at=None,
                recorded_at=moment,
            )
            if journal.status != JournalStatus.DRAFT:
                raise JournalRefused("journal_state_refused", journal.status.value.lower())
            row = ManualJournal(
                tenant_id=tenant_id,
                dual_control=governance.dual_control(db, tenant_id),
                journal_id=journal.id,
                origin=m.ORIGIN_MANUAL,
                state=m.STATE_PREPARED,
                evidence_ref=evidence,
                prepared_by_party_id=actor,
                prepared_at=moment,
            )
            db.add(row)
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_PREPARED,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=journal.id,
                number=journal.number,
                evidence_ref=evidence,
                rounding_bridge=command.bridge_sha256,
                rounding_period=command.period,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


# --- request post ----------------------------------------------------------


def request_post(db: Session, *, tenant_id: UUID, manual_id: UUID, actor: UUID, now: datetime | None = None) -> Outcome:
    """PREPARED -> POST_REQUESTED. Replays when already requested by the same actor."""
    moment = _now(now)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            row = _lock_row(db, tenant_id, manual_id)
            if row.state == m.STATE_POST_REQUESTED and row.requested_by_party_id == actor:
                return _outcome(row, replayed=True)
            if row.state != m.STATE_PREPARED:
                raise JournalRefused("state_refused", f"cannot request posting from state {row.state}")
            journal = _journal(db, tenant_id, row.journal_id)
            if journal.status != JournalStatus.DRAFT:
                raise JournalRefused("journal_state_refused", journal.status.value.lower())
            _require_open_period(db, tenant_id, journal.posting_date, journal.kind)
            _control_guard(db, tenant_id, journal=journal)
            row.state = m.STATE_POST_REQUESTED
            row.requested_by_party_id = actor
            row.requested_at = moment
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_POST_REQUESTED,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=row.journal_id,
                origin=row.origin,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


def request_legacy_post(
    db: Session, *, tenant_id: UUID, journal_id: UUID, actor: UUID, review_ref: str, now: datetime | None = None
) -> Outcome:
    """Open the maker-checker path for ONE held legacy DRAFT journal (Finance review reference).

    A previously REJECTED held journal may be re-requested with a new review reference and
    requester (SoD re-checked at approval); the prior rejection stays in the audit trail.
    """
    moment = _now(now)
    evidence = _text(review_ref, "Finance review reference", 255)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            journal = _journal(db, tenant_id, journal_id)
            existing = db.scalar(
                select(ManualJournal)
                .where(ManualJournal.tenant_id == tenant_id, ManualJournal.journal_id == journal_id)
                .with_for_update()
            )
            if journal.source_owner != LEGACY_OWNER:
                raise JournalRefused("not_legacy_held", "only journals from the legacy import can be reviewed here")
            if existing is not None:
                if existing.origin == m.ORIGIN_LEGACY_HELD and existing.requested_by_party_id == actor:
                    if existing.state == m.STATE_POST_REQUESTED and existing.evidence_ref == evidence:
                        return _outcome(existing, replayed=True)
                if existing.origin != m.ORIGIN_LEGACY_HELD or existing.state != m.STATE_REJECTED:
                    raise JournalRefused("already_requested", "this journal already has a posting workflow")
            if journal.status != JournalStatus.DRAFT:
                raise JournalRefused("journal_state_refused", journal.status.value.lower())
            _require_open_period(db, tenant_id, journal.posting_date, journal.kind)
            _control_guard(db, tenant_id, journal=journal)
            if existing is not None:  # re-request after rejection
                prior = (existing.rejected_by_party_id, existing.rejection_reason, existing.evidence_ref)
                existing.state = m.STATE_POST_REQUESTED
                existing.evidence_ref = evidence
                existing.prepared_by_party_id = actor
                existing.prepared_at = moment
                existing.requested_by_party_id = actor
                existing.requested_at = moment
                existing.rejected_by_party_id = None
                existing.rejected_at = None
                existing.rejection_reason = None
                row = existing
                extra = {
                    "re_request": True,
                    "prior_rejected_by": prior[0],
                    "prior_reason": prior[1],
                    "prior_review_ref": prior[2],
                }
            else:
                row = ManualJournal(
                    tenant_id=tenant_id,
                    dual_control=governance.dual_control(db, tenant_id),
                    journal_id=journal.id,
                    origin=m.ORIGIN_LEGACY_HELD,
                    state=m.STATE_POST_REQUESTED,
                    evidence_ref=evidence,
                    legacy_source_key=":".join(
                        (
                            journal.source_owner or "",
                            journal.source_document_kind or "",
                            journal.source_document_id or "",
                            journal.source_version or "",
                        )
                    )[:480],
                    prepared_by_party_id=actor,
                    prepared_at=moment,
                    requested_by_party_id=actor,
                    requested_at=moment,
                )
                db.add(row)
                extra = {}
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_POST_REQUESTED,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=journal.id,
                origin=row.origin,
                number=journal.number,
                review_ref=evidence,
                **extra,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


# --- approve + post / reject -----------------------------------------------


def approve_and_post(
    db: Session, *, tenant_id: UUID, manual_id: UUID, approver: UUID, now: datetime | None = None
) -> Outcome:
    """POST_REQUESTED -> POSTED through accounting.post_journal. Approver != preparer and != requester."""
    moment = _now(now)
    try:
        _actor(db, tenant_id, approver)
        with db.begin_nested():
            row = _lock_row(db, tenant_id, manual_id)
            if row.state == m.STATE_POSTED and row.approved_by_party_id == approver:
                return _outcome(row, replayed=True)
            if row.state != m.STATE_POST_REQUESTED:
                raise JournalRefused("state_refused", f"cannot approve from state {row.state}")
            if row.dual_control and approver == row.prepared_by_party_id:
                raise JournalRefused("sod_preparer_cannot_approve", "the preparer cannot approve their own journal")
            if row.dual_control and approver == row.requested_by_party_id:
                raise JournalRefused("sod_requester_cannot_approve", "the requester cannot approve their own request")
            journal = _journal(db, tenant_id, row.journal_id)
            if journal.status != JournalStatus.DRAFT:
                raise JournalRefused("journal_state_refused", journal.status.value.lower())
            _require_open_period(db, tenant_id, journal.posting_date, journal.kind)
            _control_guard(db, tenant_id, journal=journal)
            posted = accounting.post_journal(
                db,
                scope=_scope(tenant_id),
                journal_id=journal.id,
                approval_reference=f"erp.manual:{row.id}:post",
                posted_by=str(approver),
                idempotency_key=f"erp.manual:{row.id}:post",
                idempotency_expires_at=None,
                posted_at=moment,
            )
            if posted.status != JournalStatus.POSTED:
                raise JournalRefused("journal_state_refused", posted.status.value.lower())
            row.state = m.STATE_POSTED
            row.approved_by_party_id = approver
            row.approved_at = moment
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=approver,
                action=AUDIT_POSTED,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=row.journal_id,
                number=posted.number,
                origin=row.origin,
                prepared_by=row.prepared_by_party_id,
                requested_by=row.requested_by_party_id,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


def reject(
    db: Session, *, tenant_id: UUID, manual_id: UUID, actor: UUID, reason: str, now: datetime | None = None
) -> Outcome:
    """POST_REQUESTED -> REJECTED (terminal; the DRAFT journal stays unposted), or
    REVERSAL_REQUESTED -> POSTED (the reversal request is withdrawn)."""
    moment = _now(now)
    why = _text(reason, "reason", 2000)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            row = _lock_row(db, tenant_id, manual_id)
            if row.state == m.STATE_POST_REQUESTED:
                row.state = m.STATE_REJECTED
                row.rejected_by_party_id = actor
                row.rejected_at = moment
                row.rejection_reason = why
                action = AUDIT_REJECTED
            elif row.state == m.STATE_REVERSAL_REQUESTED:
                row.state = m.STATE_POSTED
                row.reversal_requested_by_party_id = None
                row.reversal_requested_at = None
                row.reversal_reason = None
                row.reversal_posting_date = None
                action = AUDIT_REVERSAL_REJECTED
            else:
                raise JournalRefused("state_refused", f"nothing to reject in state {row.state}")
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=action,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=row.journal_id,
                reason=why,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


# --- reversal --------------------------------------------------------------


def request_reversal(
    db: Session,
    *,
    tenant_id: UUID,
    manual_id: UUID,
    actor: UUID,
    reason: str,
    posting_date: date,
    now: datetime | None = None,
) -> Outcome:
    moment = _now(now)
    why = _text(reason, "reversal reason", 2000)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            row = _lock_row(db, tenant_id, manual_id)
            if (
                row.state == m.STATE_REVERSAL_REQUESTED
                and row.reversal_requested_by_party_id == actor
                and row.reversal_posting_date == posting_date
                and row.reversal_reason == why
            ):
                return _outcome(row, replayed=True)
            if row.state != m.STATE_POSTED:
                raise JournalRefused("state_refused", f"cannot request reversal from state {row.state}")
            _require_open_period(db, tenant_id, posting_date, JournalKind.REVERSAL)
            _control_guard(db, tenant_id, journal=_journal(db, tenant_id, row.journal_id), reversal=True)
            row.state = m.STATE_REVERSAL_REQUESTED
            row.reversal_requested_by_party_id = actor
            row.reversal_requested_at = moment
            row.reversal_reason = why
            row.reversal_posting_date = posting_date
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_REVERSAL_REQUESTED,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=row.journal_id,
                reason=why,
                posting_date=posting_date,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


def approve_reversal(
    db: Session, *, tenant_id: UUID, manual_id: UUID, approver: UUID, now: datetime | None = None
) -> Outcome:
    """REVERSAL_REQUESTED -> REVERSED through accounting.reverse_journal. Approver != requester and != preparer."""
    moment = _now(now)
    try:
        _actor(db, tenant_id, approver)
        with db.begin_nested():
            row = _lock_row(db, tenant_id, manual_id)
            if row.state == m.STATE_REVERSED and row.reversal_approved_by_party_id == approver:
                return _outcome(row, replayed=True)
            if row.state != m.STATE_REVERSAL_REQUESTED:
                raise JournalRefused("state_refused", f"cannot approve a reversal from state {row.state}")
            if row.dual_control and approver == row.reversal_requested_by_party_id:
                raise JournalRefused("sod_requester_cannot_approve", "the requester cannot approve their own reversal")
            if row.dual_control and approver == row.prepared_by_party_id:
                raise JournalRefused("sod_preparer_cannot_approve", "the preparer cannot approve the reversal")
            if row.reversal_posting_date is None or row.reversal_reason is None:
                raise JournalRefused("state_refused", "the reversal request is incomplete")
            _require_open_period(db, tenant_id, row.reversal_posting_date, JournalKind.REVERSAL)
            original = _journal(db, tenant_id, row.journal_id)
            _control_guard(db, tenant_id, journal=original, reversal=True)
            reversal = accounting.reverse_journal(
                db,
                scope=_scope(tenant_id),
                command=ReverseJournal(
                    journal_id=original.id,
                    number="RV-" + row.id.hex[:24].upper(),
                    posting_date=row.reversal_posting_date,
                    reason=row.reversal_reason,
                    approval_reference=f"erp.manual:{row.id}:reverse",
                    reversed_by=str(approver),
                ),
                idempotency_key=f"erp.manual:{row.id}:reverse",
                idempotency_expires_at=None,
                reversed_at=moment,
            )
            row.state = m.STATE_REVERSED
            row.reversal_approved_by_party_id = approver
            row.reversal_approved_at = moment
            row.reversal_journal_id = reversal.id
            db.flush()
            _audit(
                db,
                tenant_id=tenant_id,
                actor=approver,
                action=AUDIT_REVERSED,
                entity_type="manual_journal",
                entity_id=str(row.id),
                journal_id=row.journal_id,
                reversal_journal_id=reversal.id,
                requested_by=row.reversal_requested_by_party_id,
            )
            return _outcome(row)
    except _REFUSABLE as exc:
        raise _map(exc) from exc


# --- periods ---------------------------------------------------------------


def _period(db: Session, tenant_id: UUID, period_id: UUID) -> FiscalPeriod:
    period = db.scalar(select(FiscalPeriod).where(FiscalPeriod.tenant_id == tenant_id, FiscalPeriod.id == period_id))
    if period is None:
        raise JournalRefused("not_found", "fiscal period was not found")
    return period


def open_period(db: Session, *, tenant_id: UUID, period_id: UUID, actor: UUID, now: datetime | None = None) -> str:
    moment = _now(now)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            _period(db, tenant_id, period_id)
            period = accounting.open_period(
                db, scope=_scope(tenant_id), period_id=period_id, actor_ref=str(actor), occurred_at=moment
            )
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_PERIOD_OPENED,
                entity_type="fiscal_period",
                entity_id=str(period_id),
                name=period.name,
            )
            return period.status.value
    except _REFUSABLE as exc:
        raise _map(exc) from exc


def pending_manual_journals(db: Session, tenant_id: UUID, period_id: UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(ManualJournal)
            .join(
                JournalEntry,
                (JournalEntry.tenant_id == ManualJournal.tenant_id) & (JournalEntry.id == ManualJournal.journal_id),
            )
            .where(
                ManualJournal.tenant_id == tenant_id,
                JournalEntry.fiscal_period_id == period_id,
                ManualJournal.state.in_((m.STATE_PREPARED, m.STATE_POST_REQUESTED, m.STATE_REVERSAL_REQUESTED)),
            )
        )
        or 0
    )


def soft_close_period(
    db: Session, *, tenant_id: UUID, period_id: UUID, actor: UUID, review_ref: str, now: datetime | None = None
) -> str:
    """OPEN -> SOFT_CLOSED with accounting's close evidence: no pending manual journals + a Finance review reference."""
    moment = _now(now)
    review = _text(review_ref, "Finance review reference", 255)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            row = _period(db, tenant_id, period_id)
            from erp_v2.billing_gl.checks import facts_incomplete

            if facts_incomplete(db, tenant_id, row.start_date, row.end_date, now=moment):
                raise JournalRefused("billing_facts_incomplete", "billing accounting facts are not fully mapped")
            pending = pending_manual_journals(db, tenant_id, period_id)
            if pending:
                raise JournalRefused("pending_manual_journals", f"{pending} manual journal(s) are still unresolved")
            auto = f"erp.manual.pending=0:{period_id}"
            period = accounting.soft_close_period(
                db,
                scope=_scope(tenant_id),
                period_id=period_id,
                evidence=PeriodCloseEvidence(
                    checks=(
                        CloseCheck("NO_PENDING_MANUAL_JOURNALS", True, auto, hashlib.sha256(auto.encode()).hexdigest()),
                        CloseCheck("FINANCE_REVIEW", True, review, hashlib.sha256(review.encode()).hexdigest()),
                    )
                ),
                actor_ref=str(actor),
                approval_reference=f"erp.period:{period_id}:soft_close",
                occurred_at=moment,
            )
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_PERIOD_SOFT_CLOSED,
                entity_type="fiscal_period",
                entity_id=str(period_id),
                review_ref=review,
            )
            return period.status.value
    except _REFUSABLE as exc:
        raise _map(exc) from exc


def lock_period(db: Session, *, tenant_id: UUID, period_id: UUID, actor: UUID, now: datetime | None = None) -> str:
    moment = _now(now)
    try:
        _actor(db, tenant_id, actor)
        with db.begin_nested():
            row = _period(db, tenant_id, period_id)
            from erp_v2.billing_gl.checks import facts_incomplete

            if facts_incomplete(db, tenant_id, row.start_date, row.end_date, now=moment):
                raise JournalRefused("billing_facts_incomplete", "billing accounting facts are not fully mapped")
            period = accounting.lock_period(
                db,
                scope=_scope(tenant_id),
                period_id=period_id,
                actor_ref=str(actor),
                approval_reference=f"erp.period:{period_id}:lock",
                occurred_at=moment,
            )
            _audit(
                db,
                tenant_id=tenant_id,
                actor=actor,
                action=AUDIT_PERIOD_LOCKED,
                entity_type="fiscal_period",
                entity_id=str(period_id),
            )
            return period.status.value
    except _REFUSABLE as exc:
        raise _map(exc) from exc
