"""Staff loan entries: prepare, approve, record (PR-3).

Four kinds (see ``models``): an ``advance`` pays a new loan from a bank account (Dr 1410 / Cr the bank's cash
GL); an ``opening`` carries an employee's existing balance into the subledger (no GL, the GL already holds
it); an ``adjustment`` moves one employee's balance against an offset account; a ``control_difference``
writes off part of the measured difference between GL(1410) and the subledger.

Every kind goes draft -> approved -> recorded. There is NO compulsory second person: the approver (and the
recorder) may be the preparer. The approval is still an explicit, audited action with its own evidence on
the row. Recording posts through the P0 bridge (new ``("erp","staff_loan_entry")`` RefPolicy) in the SAME
savepoint as the state change, so a refusal leaves no row change and no journal.

Reused, not reinvented: ``supplier_payments.service.payer_references`` (cash GL from the selected bank
account, never free text), ``bank_reference.claim`` (one bank reference per account across B3, B5 and the
loans), the V3 approved EMPLOYEE payee instruction, and the B3 value-date rule (not in the future).

Opening bound: the sum of all employee balances after the opening must not exceed GL(1410). A
control-difference write-off is at most the measured difference GL(1410) - subledger and only while that
difference is positive.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_accounting.contracts import AccountKind
from dotmac_accounting.models import Account
from dotmac_banking.models import BankAccount
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from dotmac_kernel.models import Party, Tenant
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.disbursement import bank_reference as bank_ref
from erp_v2.journals import policy as journal_policy
from erp_v2.ledger_roles import STAFF_LOAN_CODE, account_id_for_code, control_codes_among
from erp_v2.payees import service as payee_service
from erp_v2.payees.models import PayeeKind
from erp_v2.posting.bridge import post_consequence
from erp_v2.posting.rules import PostingRefused
from erp_v2.staff_loans import queries
from erp_v2.staff_loans.models import (
    APPROVED,
    DRAFT,
    KIND_ADJUSTMENT,
    KIND_ADVANCE,
    KIND_CONTROL_DIFFERENCE,
    KIND_OPENING,
    RECORDED,
    StaffLoanEntry,
)
from erp_v2.supplier_payments.service import PaymentRefused, payer_references

AUDIT_PREPARED: Final = "erp.finance.staff_loan_entry.prepared"
AUDIT_APPROVED: Final = "erp.finance.staff_loan_entry.approved"
AUDIT_RECORDED: Final = "erp.finance.staff_loan_entry.recorded"
AUDIT_REFUSED: Final = "erp.finance.staff_loan_entry.refused"
AUDIT_ACTIONS: Final = (AUDIT_PREPARED, AUDIT_APPROVED, AUDIT_RECORDED, AUDIT_REFUSED)

OWNER: Final = "erp"
DOCUMENT_KIND: Final = "staff_loan_entry"
SOURCE_VERSION: Final = "1"
_QUANTUM: Final = Decimal("0.000001")
_ZERO: Final = Decimal(0)


class LoanRefused(ValueError):
    """A safe, user-facing refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    entry_id: UUID
    replayed: bool
    journal_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class _Source:
    owner: str
    document_kind: str
    document_id: str
    version: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _Line:
    account_ref: str
    debit: Decimal
    credit: Decimal
    description: str | None
    dimension_refs: tuple = ()


@dataclass(frozen=True, slots=True)
class _Consequence:
    source: _Source
    posting_date: date
    description: str
    currency_code: str
    exchange_rate: Decimal
    entries: tuple[_Line, ...]
    fingerprint: str


# --- helpers -------------------------------------------------------------------------------------


def _audit(
    db: Session, *, tenant: Tenant, party: Party, entity_id: str, action: str, success: bool, details: dict[str, object]
) -> None:
    write_audit_event(
        db,
        tenant_id=tenant.id,
        actor_party_id=party.id,
        actor_type="user",
        actor_id=str(party.id),
        actor_label=getattr(party, "email", None) or None,
        source_application="dotmac_erp",
        action=action,
        entity_type="staff_loan_entry",
        entity_id=entity_id,
        is_success=success,
        details=details,
    )


def _printable(value: str, *, maximum: int) -> bool:
    return 0 < len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 or 0x80 <= ord(c) <= 0x9F for c in value)


def _clean_text(value: str | None, *, code: str, label: str, maximum: int, required: bool = True) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        if required:
            raise LoanRefused(code, f"{label} is required")
        return None
    if not _printable(cleaned, maximum=maximum):
        raise LoanRefused(code, f"{label} must be printable text of at most {maximum} characters")
    return cleaned


def _amount(value: Decimal, *, signed: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value == 0 or value != value.quantize(_QUANTUM):
        raise LoanRefused("invalid_amount", "The amount must be a non-zero number with at most six decimals")
    if value < 0 and not signed:
        raise LoanRefused("invalid_amount", "The amount must be positive")
    return value


def _functional_currency(db: Session, tenant_id: UUID) -> str:
    try:
        return journal_policy.functional_currency(db, tenant_id)
    except journal_policy.FunctionalCurrencyError as exc:
        raise LoanRefused("functional_currency_unset", "The tenant has no functional currency setting") from exc


def _loan_account_id(db: Session, tenant_id: UUID) -> UUID:
    found = account_id_for_code(db, tenant_id, STAFF_LOAN_CODE)
    if found is None:
        raise LoanRefused("staff_loan_account_unavailable", "The 1410 Staff Loan account is not set up for this tenant")
    return found


def _present(value, label: str):
    """The shape CHECKs guarantee these per kind; refuse (never assert) if a row ever breaks that."""
    if value is None:
        raise LoanRefused("entry_malformed", f"The staff loan entry has no {label}")
    return value


def _locked_entry(db: Session, tenant_id: UUID, entry_id: UUID) -> StaffLoanEntry:
    row = db.scalar(
        select(StaffLoanEntry)
        .where(StaffLoanEntry.tenant_id == tenant_id, StaffLoanEntry.id == entry_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise LoanRefused("not_found", "Staff loan entry was not found")
    return row


def _offset_account(db: Session, tenant_id: UUID, account_id: UUID) -> Account:
    account = db.scalar(select(Account).where(Account.tenant_id == tenant_id, Account.id == account_id))
    if account is None:
        raise LoanRefused("offset_unknown", "The offset account was not found")
    if not account.is_active or not account.posting_allowed or account.kind != AccountKind.POSTING:
        raise LoanRefused("offset_not_postable", "The offset account is not open for posting")
    if control_codes_among(db, tenant_id, [account.id]):
        raise LoanRefused(
            "offset_is_control_account", "The offset may not be a control account (AP, payroll payable or staff loan)"
        )
    cash = db.scalar(
        select(BankAccount.id)
        .where(BankAccount.tenant_id == tenant_id, BankAccount.cash_account_ref.in_((str(account.id), account.code)))
        .limit(1)
    )
    if cash is not None:
        raise LoanRefused("offset_is_cash_account", "The offset may not be a bank account's cash ledger account")
    return account


def _require_instruction(db: Session, tenant_id: UUID, employee_ref: str) -> None:
    """V3: the borrower must have an approved EMPLOYEE payee bank instruction before money is paid."""
    try:
        payee_service.destination_for_payment(
            db, tenant_id=tenant_id, payee_kind=PayeeKind.EMPLOYEE.value, payee_ref=employee_ref
        )
    except payee_service.PayeeBankError as exc:
        raise LoanRefused(
            "payee_instruction_required",
            "The employee has no approved bank instruction; propose and approve one before paying a loan",
        ) from exc


def _verify(db: Session, tenant_id: UUID, row: StaffLoanEntry) -> None:
    """Every business check for one entry, under the tenant's staff-loan lock. Used at prepare, approve, record."""
    queries.lock_book(db, tenant_id)
    _loan_account_id(db, tenant_id)
    if row.kind == KIND_ADVANCE:
        functional = _functional_currency(db, tenant_id)
        try:
            payer_references(
                db,
                tenant_id=tenant_id,
                bank_account_id=_present(row.bank_account_id, "bank account"),
                currency=functional,
            )
        except PaymentRefused as exc:
            raise LoanRefused(exc.code, str(exc)) from exc
    elif row.kind in (KIND_ADJUSTMENT, KIND_CONTROL_DIFFERENCE):
        _functional_currency(db, tenant_id)
        _offset_account(db, tenant_id, _present(row.offset_account_id, "offset account"))
    employee = row.employee_ref
    if employee is not None:
        balance = queries.employee_balance(db, tenant_id=tenant_id, employee_ref=employee)
        if row.kind == KIND_OPENING:
            _verify_opening(db, tenant_id, row, balance)
        elif balance.opening_date is not None and row.value_date < balance.opening_date:
            raise LoanRefused(
                "entry_before_opening",
                "The entry is dated before the employee's opening balance, which already includes it",
            )
        if row.kind == KIND_ADJUSTMENT and row.amount < 0 and balance.balance + row.amount < 0:
            raise LoanRefused(
                "adjustment_exceeds_balance", "The adjustment would take the employee's loan balance below zero"
            )
    else:
        position = queries.book_position(db, tenant_id=tenant_id)
        if position.difference <= 0 or -row.amount > position.difference:
            raise LoanRefused(
                "control_difference_exceeds",
                "The write-off exceeds the measured difference between GL 1410 and the staff-loan subledger "
                f"({position.difference})",
            )


def _verify_opening(db: Session, tenant_id: UUID, row: StaffLoanEntry, balance: queries.LoanBalance) -> None:
    if balance.opening_date is not None and balance.opening_date != row.value_date:
        raise LoanRefused(
            "opening_date_mismatch",
            "All of an employee's openings share one value date (the date of Finance's schedule)",
        )
    earlier = db.scalar(
        select(StaffLoanEntry.id)
        .where(
            StaffLoanEntry.tenant_id == tenant_id,
            StaffLoanEntry.employee_ref == row.employee_ref,
            StaffLoanEntry.state == RECORDED,
            StaffLoanEntry.kind.in_((KIND_ADVANCE, KIND_ADJUSTMENT)),
            StaffLoanEntry.value_date < row.value_date,
        )
        .limit(1)
    )
    if earlier is not None:
        raise LoanRefused(
            "opening_after_entries", "The employee already has a recorded entry dated before this opening"
        )
    position = queries.book_position(db, tenant_id=tenant_id)
    headroom = position.gl_balance - position.subledger_total
    if row.amount > headroom:
        raise LoanRefused(
            "opening_exceeds_gl",
            "The openings would exceed the GL balance of 1410 "
            f"(headroom {max(headroom, _ZERO)}); the subledger can never exceed the control account",
        )


def _same_request(row: StaffLoanEntry, **expected: object) -> bool:
    return all(getattr(row, name) == value for name, value in expected.items())


# --- prepare -------------------------------------------------------------------------------------


def _prepare(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    kind: str,
    employee_ref: str | None,
    amount: Decimal,
    value_date: date,
    evidence_ref: str,
    description: str | None,
    bank_account_id: UUID | None,
    offset_account_id: UUID | None,
    now: datetime,
) -> EntryOutcome:
    employee = (
        None
        if kind == KIND_CONTROL_DIFFERENCE
        else _clean_text(employee_ref, code="employee_required", label="The employee reference", maximum=240)
    )
    evidence = _clean_text(evidence_ref, code="evidence_required", label="The evidence reference", maximum=240)
    note = _clean_text(description, code="description_invalid", label="The description", maximum=500, required=False)
    stored_amount = _amount(amount, signed=kind in (KIND_ADJUSTMENT, KIND_CONTROL_DIFFERENCE))
    if kind == KIND_CONTROL_DIFFERENCE:
        stored_amount = -abs(stored_amount)  # a write-off credits 1410
    existing = db.scalar(
        select(StaffLoanEntry).where(StaffLoanEntry.tenant_id == tenant.id, StaffLoanEntry.request_id == request_id)
    )
    if existing is not None:
        if not _same_request(
            existing,
            kind=kind,
            employee_ref=employee,
            amount=stored_amount,
            value_date=value_date,
            evidence_ref=evidence,
            bank_account_id=bank_account_id,
            offset_account_id=offset_account_id,
        ):
            raise LoanRefused("request_conflict", "This request id was already used for a different entry")
        return EntryOutcome(existing.id, True)
    row = StaffLoanEntry(
        tenant_id=tenant.id,
        request_id=request_id,
        employee_ref=employee,
        kind=kind,
        amount=stored_amount,
        evidence_ref=evidence,
        description=note,
        value_date=value_date,
        bank_account_id=bank_account_id,
        offset_account_id=offset_account_id,
        state=DRAFT,
        prepared_by_party_id=party.id,
        prepared_at=now,
    )
    _verify(db, tenant.id, row)
    db.add(row)
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(row.id),
        action=AUDIT_PREPARED,
        success=True,
        details={
            "kind": kind,
            "employee_ref": employee,
            "amount": str(stored_amount),
            "value_date": value_date.isoformat(),
            "evidence_ref": evidence,
        },
    )
    return EntryOutcome(row.id, False)


# --- approve -------------------------------------------------------------------------------------


def _approve(db: Session, *, tenant: Tenant, party: Party, entry_id: UUID, now: datetime) -> EntryOutcome:
    row = _locked_entry(db, tenant.id, entry_id)
    if row.state in (APPROVED, RECORDED) and row.approved_by_party_id == party.id:
        return EntryOutcome(row.id, True, row.journal_id)
    if row.state != DRAFT:
        raise LoanRefused("not_draft", "Only a draft staff loan entry can be approved")
    _verify(db, tenant.id, row)
    if row.kind == KIND_ADVANCE:
        _require_instruction(db, tenant.id, _present(row.employee_ref, "employee"))
    row.state = APPROVED
    row.approved_by_party_id = party.id
    row.approved_at = now
    row.updated_at = now
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(row.id),
        action=AUDIT_APPROVED,
        success=True,
        details={"kind": row.kind, "prepared_by": str(row.prepared_by_party_id)},
    )
    return EntryOutcome(row.id, False)


# --- record --------------------------------------------------------------------------------------


def _fingerprint(tenant_id: UUID, row: StaffLoanEntry, accounts: list[tuple[str, str, str]]) -> str:
    material = {
        "tenant": str(tenant_id),
        "entry": str(row.id),
        "kind": row.kind,
        "employee": row.employee_ref,
        "amount": str(row.amount),
        "value_date": row.value_date.isoformat(),
        "lines": accounts,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _consequence(db: Session, tenant_id: UUID, row: StaffLoanEntry, loan_id: UUID, functional: str) -> _Consequence:
    magnitude = abs(row.amount)
    if row.kind == KIND_ADVANCE:
        cash_ref, _ = payer_references(
            db, tenant_id=tenant_id, bank_account_id=_present(row.bank_account_id, "bank account"), currency=functional
        )
        other, label = cash_ref, "Staff loan advance"
    else:
        other = str(_offset_account(db, tenant_id, _present(row.offset_account_id, "offset account")).id)
        label = "Staff loan adjustment" if row.kind == KIND_ADJUSTMENT else "Staff loan control difference write-off"
    toward_loan = row.amount > 0  # Dr 1410 raises the balance
    description = f"{label} {row.employee_ref or 'control'} [{row.evidence_ref}]"[:2000].strip()
    loan = _Line(str(loan_id), magnitude if toward_loan else _ZERO, _ZERO if toward_loan else magnitude, description)
    counter = _Line(other, _ZERO if toward_loan else magnitude, magnitude if toward_loan else _ZERO, description)
    lines = (loan, counter)
    fingerprint = _fingerprint(
        tenant_id, row, [(line.account_ref, str(line.debit), str(line.credit)) for line in lines]
    )
    source = _Source(OWNER, DOCUMENT_KIND, str(row.id), SOURCE_VERSION, fingerprint)
    return _Consequence(source, row.value_date, description, functional, Decimal(1), lines, fingerprint)


def _record(
    db: Session, *, tenant: Tenant, party: Party, entry_id: UUID, bank_reference: str | None, now: datetime
) -> EntryOutcome:
    row = _locked_entry(db, tenant.id, entry_id)
    if row.state == RECORDED:
        if row.kind == KIND_ADVANCE:
            try:
                reference = bank_ref.normalize(bank_reference)
            except bank_ref.BankReferenceInvalid:
                reference = None
            if row.bank_reference != reference:
                raise LoanRefused("already_recorded", "This entry was already recorded with a different bank reference")
        return EntryOutcome(row.id, True, row.journal_id)
    if row.state != APPROVED:
        raise LoanRefused("not_approved", "Only an approved staff loan entry can be recorded")
    if row.value_date > now.astimezone(UTC).date():
        raise LoanRefused(
            "value_date_future", "The entry's value date is in the future; record it on or after that date"
        )
    _verify(db, tenant.id, row)
    scope = TenantScope(tenant.id)
    actor = str(party.id)
    posted = None
    if row.kind == KIND_ADVANCE:
        _require_instruction(db, tenant.id, _present(row.employee_ref, "employee"))
        bank_account_id = _present(row.bank_account_id, "bank account")
        try:
            reference = bank_ref.normalize(bank_reference)
        except bank_ref.BankReferenceInvalid as exc:
            raise LoanRefused(
                "bank_reference_invalid", "Enter the bank's evidence reference (3 to 255 printable characters)"
            ) from exc
        try:
            bank_ref.claim(db, tenant_id=tenant.id, bank_account_id=bank_account_id, reference=reference)
        except bank_ref.BankReferenceReused as exc:
            raise LoanRefused(
                "bank_reference_reused", "This bank reference was already used on this bank account"
            ) from exc
        row.bank_reference = reference
    elif bank_reference is not None and bank_reference.strip():
        raise LoanRefused("bank_reference_not_applicable", "Only an advance carries a bank reference")
    if row.kind != KIND_OPENING:
        functional = _functional_currency(db, tenant.id)
        consequence = _consequence(db, tenant.id, row, _loan_account_id(db, tenant.id), functional)
        approval_ref = f"erp.staff_loan:{row.id}:approver:{row.approved_by_party_id}:recorder:{actor}"
        posted = post_consequence(
            db, scope=scope, consequence=consequence, approval_ref=approval_ref, actor=actor, now=now
        )
        row.journal_id = posted.journal_id
    row.state = RECORDED
    row.recorded_by_party_id = party.id
    row.recorded_at = now
    row.updated_at = now
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(row.id),
        action=AUDIT_RECORDED,
        success=True,
        details={
            "kind": row.kind,
            "employee_ref": row.employee_ref,
            "amount": str(row.amount),
            "bank_reference": row.bank_reference,
            "journal_id": str(posted.journal_id) if posted else None,
            "journal_number": posted.journal_number if posted else None,
            "prepared_by": str(row.prepared_by_party_id),
            "approved_by": str(row.approved_by_party_id),
        },
    )
    return EntryOutcome(row.id, False, row.journal_id)


# --- public commands -----------------------------------------------------------------------------


def _guard(db: Session, *, tenant: Tenant, party: Party, entity_id: str, action: str, work):
    """Run ``work`` in one savepoint; on refusal roll it back and audit the refusal."""
    if party.tenant_id != tenant.id:
        raise LoanRefused("actor_tenant", "The acting party must belong to this tenant")
    try:
        with db.begin_nested():
            return work()
    except LoanRefused as exc:
        refusal = exc
    except PostingRefused as exc:
        refusal = LoanRefused(f"posting_{exc.code}", f"The ledger refused the posting ({exc.code})")
    except IdempotencyConflict as exc:
        refusal = LoanRefused("idempotency_conflict", str(exc))
    except IntegrityError:
        refusal = LoanRefused("conflict", "A concurrent change prevented the action; reload and retry")
    if refusal.code != "not_found":
        _audit(
            db,
            tenant=tenant,
            party=party,
            entity_id=entity_id,
            action=AUDIT_REFUSED,
            success=False,
            details={"code": refusal.code, "attempted": action},
        )
    raise refusal


def _prepare_command(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    kind: str,
    employee_ref: str | None,
    amount: Decimal,
    value_date: date,
    evidence_ref: str,
    description: str | None = None,
    bank_account_id: UUID | None = None,
    offset_account_id: UUID | None = None,
    now: datetime | None = None,
) -> EntryOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(request_id),
        action=f"prepare_{kind}",
        work=lambda: _prepare(
            db,
            tenant=tenant,
            party=party,
            request_id=request_id,
            kind=kind,
            employee_ref=employee_ref,
            amount=amount,
            value_date=value_date,
            evidence_ref=evidence_ref,
            description=description,
            bank_account_id=bank_account_id,
            offset_account_id=offset_account_id,
            now=moment,
        ),
    )


def prepare_advance(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    employee_ref: str,
    amount: Decimal,
    bank_account_id: UUID,
    value_date: date,
    evidence_ref: str,
    description: str | None = None,
    now: datetime | None = None,
) -> EntryOutcome:
    """A new loan paid from ``bank_account_id`` (Dr 1410 / Cr that account's cash GL at record)."""
    return _prepare_command(
        db,
        tenant=tenant,
        party=party,
        request_id=request_id,
        kind=KIND_ADVANCE,
        employee_ref=employee_ref,
        amount=amount,
        value_date=value_date,
        evidence_ref=evidence_ref,
        description=description,
        bank_account_id=bank_account_id,
        now=now,
    )


def prepare_opening(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    employee_ref: str,
    amount: Decimal,
    value_date: date,
    evidence_ref: str,
    description: str | None = None,
    now: datetime | None = None,
) -> EntryOutcome:
    """An opening balance from Finance's schedule: subledger only, no GL."""
    return _prepare_command(
        db,
        tenant=tenant,
        party=party,
        request_id=request_id,
        kind=KIND_OPENING,
        employee_ref=employee_ref,
        amount=amount,
        value_date=value_date,
        evidence_ref=evidence_ref,
        description=description,
        now=now,
    )


def prepare_adjustment(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    employee_ref: str,
    amount: Decimal,
    offset_account_id: UUID,
    value_date: date,
    evidence_ref: str,
    description: str | None = None,
    now: datetime | None = None,
) -> EntryOutcome:
    """Signed: positive is Dr 1410 / Cr offset, negative is Dr offset / Cr 1410."""
    return _prepare_command(
        db,
        tenant=tenant,
        party=party,
        request_id=request_id,
        kind=KIND_ADJUSTMENT,
        employee_ref=employee_ref,
        amount=amount,
        value_date=value_date,
        evidence_ref=evidence_ref,
        description=description,
        offset_account_id=offset_account_id,
        now=now,
    )


def prepare_control_difference(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    amount: Decimal,
    offset_account_id: UUID,
    value_date: date,
    evidence_ref: str,
    description: str | None = None,
    now: datetime | None = None,
) -> EntryOutcome:
    """Write off ``amount`` (positive) of the GL(1410) - subledger difference: Dr offset / Cr 1410."""
    return _prepare_command(
        db,
        tenant=tenant,
        party=party,
        request_id=request_id,
        kind=KIND_CONTROL_DIFFERENCE,
        employee_ref=None,
        amount=amount,
        value_date=value_date,
        evidence_ref=evidence_ref,
        description=description,
        offset_account_id=offset_account_id,
        now=now,
    )


def approve_entry(
    db: Session, *, tenant: Tenant, party: Party, entry_id: UUID, now: datetime | None = None
) -> EntryOutcome:
    """Approval is an explicit action; the approver may be the preparer (no compulsory second person)."""
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(entry_id),
        action="approve",
        work=lambda: _approve(db, tenant=tenant, party=party, entry_id=entry_id, now=moment),
    )


def record_entry(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    entry_id: UUID,
    bank_reference: str | None = None,
    now: datetime | None = None,
) -> EntryOutcome:
    """Record (and post, except an opening). ``bank_reference`` is required for an advance only."""
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(entry_id),
        action="record",
        work=lambda: _record(
            db, tenant=tenant, party=party, entry_id=entry_id, bank_reference=bank_reference, now=moment
        ),
    )
