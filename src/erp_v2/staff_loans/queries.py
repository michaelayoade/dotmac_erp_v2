"""Read side of the staff-loan subledger: the employee balance and the 1410 book position.

Employee balance = the sum of that employee's RECORDED entries (opening, advance, adjustment) minus the
payroll internal recoveries booked against 1410 for that employee (payroll a5 ``PayrollLiability`` rows of
FINALIZED runs, read-only). An opening is a balance AS AT its value date, already net of every earlier
recovery, so only recoveries of runs whose period ends AFTER the employee's opening date count. An
employee without an opening counts every recovery.

``control_difference`` rows name no employee and never enter the subledger. The book position compares the
GL balance of 1410 (posted ledger lines) with the sum of all employee balances.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from dotmac_accounting.models import PostedLedgerLine
from dotmac_payroll.models import PayrollLiability, PayrollRun
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from erp_v2.ledger_roles import STAFF_LOAN_CODE, account_id_for_code
from erp_v2.staff_loans.models import KIND_OPENING, RECORDED, StaffLoanEntry

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class LoanBalance:
    employee_ref: str
    entries: Decimal
    recoveries: Decimal
    balance: Decimal
    opening_date: date | None


@dataclass(frozen=True, slots=True)
class BookPosition:
    """GL(1410) against the staff-loan subledger. ``difference`` > 0: the GL holds more than the subledger."""

    gl_balance: Decimal
    subledger_total: Decimal
    difference: Decimal


def lock_book(db: Session, tenant_id: UUID) -> None:
    """Serialise every staff-loan balance decision of a tenant (transaction-scoped advisory lock)."""
    digest = hashlib.sha256(f"erp_v2.staff_loans\x1f{str(tenant_id).lower()}".encode()).digest()
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": struct.unpack(">q", digest[:8])[0]})


def _recovery_refs(db: Session, tenant_id: UUID) -> set[str]:
    refs = {STAFF_LOAN_CODE}
    account_id = account_id_for_code(db, tenant_id, STAFF_LOAN_CODE)
    if account_id is not None:
        refs.add(str(account_id))
    return refs


def _opening_dates(db: Session, tenant_id: UUID, employee_ref: str | None = None) -> dict[str, date]:
    query = (
        select(StaffLoanEntry.employee_ref, func.min(StaffLoanEntry.value_date))
        .where(
            StaffLoanEntry.tenant_id == tenant_id,
            StaffLoanEntry.kind == KIND_OPENING,
            StaffLoanEntry.state == RECORDED,
        )
        .group_by(StaffLoanEntry.employee_ref)
    )
    if employee_ref is not None:
        query = query.where(StaffLoanEntry.employee_ref == employee_ref)
    return {ref: day for ref, day in db.execute(query)}


def _recoveries(
    db: Session, tenant_id: UUID, employee_ref: str | None, exclude_run_id: UUID | None
) -> list[tuple[str, Decimal, date]]:
    query = (
        select(PayrollLiability.beneficiary_ref, PayrollLiability.amount, PayrollRun.period_end)
        .join(
            PayrollRun,
            (PayrollRun.tenant_id == PayrollLiability.tenant_id) & (PayrollRun.id == PayrollLiability.run_id),
        )
        .where(
            PayrollLiability.tenant_id == tenant_id,
            PayrollLiability.settlement_mode == "internal_recovery",
            PayrollLiability.beneficiary_type == "employee",
            PayrollLiability.liability_account_ref.in_(_recovery_refs(db, tenant_id)),
            PayrollRun.status == "finalized",
        )
    )
    if employee_ref is not None:
        query = query.where(PayrollLiability.beneficiary_ref == employee_ref)
    if exclude_run_id is not None:
        query = query.where(PayrollLiability.run_id != exclude_run_id)
    return [(ref, amount, end) for ref, amount, end in db.execute(query)]


def _entry_totals(db: Session, tenant_id: UUID, employee_ref: str | None) -> dict[str, Decimal]:
    query = (
        select(StaffLoanEntry.employee_ref, func.coalesce(func.sum(StaffLoanEntry.amount), 0))
        .where(
            StaffLoanEntry.tenant_id == tenant_id,
            StaffLoanEntry.state == RECORDED,
            StaffLoanEntry.employee_ref.is_not(None),
        )
        .group_by(StaffLoanEntry.employee_ref)
    )
    if employee_ref is not None:
        query = query.where(StaffLoanEntry.employee_ref == employee_ref)
    return {ref: Decimal(total) for ref, total in db.execute(query)}


def _balances(
    db: Session, tenant_id: UUID, employee_ref: str | None, exclude_run_id: UUID | None
) -> dict[str, LoanBalance]:
    entries = _entry_totals(db, tenant_id, employee_ref)
    openings = _opening_dates(db, tenant_id, employee_ref)
    recovered: dict[str, Decimal] = {}
    for ref, amount, period_end in _recoveries(db, tenant_id, employee_ref, exclude_run_id):
        cutoff = openings.get(ref)
        if cutoff is not None and period_end <= cutoff:
            continue  # already netted in the opening balance
        recovered[ref] = recovered.get(ref, _ZERO) + amount
    result: dict[str, LoanBalance] = {}
    for ref in sorted(set(entries) | set(recovered)):
        total, taken = entries.get(ref, _ZERO), recovered.get(ref, _ZERO)
        result[ref] = LoanBalance(ref, total, taken, total - taken, openings.get(ref))
    return result


def employee_balance(
    db: Session, *, tenant_id: UUID, employee_ref: str, exclude_run_id: UUID | None = None
) -> LoanBalance:
    """The employee's loan balance (an employee with no entries and no recoveries reads 0)."""
    found = _balances(db, tenant_id, employee_ref, exclude_run_id).get(employee_ref)
    return found or LoanBalance(
        employee_ref, _ZERO, _ZERO, _ZERO, _opening_dates(db, tenant_id, employee_ref).get(employee_ref)
    )


def all_balances(db: Session, *, tenant_id: UUID) -> dict[str, LoanBalance]:
    return _balances(db, tenant_id, None, None)


def gl_balance(db: Session, *, tenant_id: UUID) -> Decimal:
    """Debit-minus-credit of the posted ledger lines on the 1410 account (0 when the account is absent)."""
    account_id = account_id_for_code(db, tenant_id, STAFF_LOAN_CODE)
    if account_id is None:
        return _ZERO
    total = db.scalar(
        select(func.coalesce(func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0)).where(
            PostedLedgerLine.tenant_id == tenant_id, PostedLedgerLine.account_id == account_id
        )
    )
    return Decimal(total)


def book_position(db: Session, *, tenant_id: UUID) -> BookPosition:
    gl = gl_balance(db, tenant_id=tenant_id)
    subledger = sum((b.balance for b in all_balances(db, tenant_id=tenant_id).values()), _ZERO)
    return BookPosition(gl, subledger, gl - subledger)


def list_entries(
    db: Session, *, tenant_id: UUID, employee_ref: str | None = None, limit: int = 200
) -> list[StaffLoanEntry]:
    query = select(StaffLoanEntry).where(StaffLoanEntry.tenant_id == tenant_id)
    if employee_ref is not None:
        query = query.where(StaffLoanEntry.employee_ref == employee_ref)
    return list(db.scalars(query.order_by(StaffLoanEntry.prepared_at.desc(), StaffLoanEntry.id).limit(limit)))
