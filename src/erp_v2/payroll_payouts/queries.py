"""Read models for the payroll payout screens. Read only, bounded; no account numbers, no bank bytes."""

from __future__ import annotations

from uuid import UUID

from dotmac_banking.models import BankAccount
from dotmac_kernel.models import Party
from dotmac_payroll.models import PayrollLiability, PayrollRun
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.payroll_payouts.models import PayrollPayoutBatch, PayrollPayoutLine
from erp_v2.payroll_payouts.service import COMPONENT_PREFIX, NET_PAY_KEY

LIMIT = 200


def register(db: Session, *, tenant_id: UUID, statuses: tuple[str, ...] | None) -> list[PayrollPayoutBatch]:
    stmt = select(PayrollPayoutBatch).where(PayrollPayoutBatch.tenant_id == tenant_id)
    if statuses:
        stmt = stmt.where(PayrollPayoutBatch.status.in_(statuses))
    return list(db.scalars(stmt.order_by(PayrollPayoutBatch.prepared_at.desc()).limit(LIMIT)))


def finalized_runs(db: Session, *, tenant_id: UUID) -> list[PayrollRun]:
    return list(
        db.scalars(
            select(PayrollRun)
            .where(PayrollRun.tenant_id == tenant_id, PayrollRun.status == "finalized")
            .order_by(PayrollRun.period_end.desc())
            .limit(50)
        )
    )


def bank_accounts(db: Session, *, tenant_id: UUID, currency: str | None) -> list[BankAccount]:
    stmt = select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.status == "active")
    if currency:
        stmt = stmt.where(BankAccount.currency_code == currency)
    return list(db.scalars(stmt.order_by(BankAccount.account_code).limit(100)))


def candidates(db: Session, *, tenant_id: UUID, run_id: UUID, kind: str) -> list[PayrollLiability]:
    """Outstanding, not yet batched liabilities of the run for the chosen kind (display only)."""
    live = select(PayrollPayoutLine.liability_id).where(
        PayrollPayoutLine.tenant_id == tenant_id, PayrollPayoutLine.active.is_(True)
    )
    stmt = select(PayrollLiability).where(
        PayrollLiability.tenant_id == tenant_id,
        PayrollLiability.run_id == run_id,
        PayrollLiability.status == "outstanding",
        PayrollLiability.id.not_in(live),
    )
    if kind == "net_pay":
        stmt = stmt.where(PayrollLiability.liability_key == NET_PAY_KEY)
    else:
        stmt = stmt.where(PayrollLiability.liability_key.like(COMPONENT_PREFIX + "%"))
    return list(db.scalars(stmt.order_by(PayrollLiability.beneficiary_ref, PayrollLiability.id).limit(LIMIT)))


def detail(db: Session, *, tenant_id: UUID, batch_id: UUID) -> dict[str, object] | None:
    batch = db.scalar(
        select(PayrollPayoutBatch).where(PayrollPayoutBatch.tenant_id == tenant_id, PayrollPayoutBatch.id == batch_id)
    )
    if batch is None:
        return None
    lines = list(
        db.scalars(
            select(PayrollPayoutLine)
            .where(PayrollPayoutLine.tenant_id == tenant_id, PayrollPayoutLine.batch_id == batch.id)
            .order_by(PayrollPayoutLine.id)
            .limit(LIMIT * 3)
        )
    )
    ids = {
        i
        for i in (
            batch.prepared_by_party_id,
            batch.approved_by_party_id,
            batch.file_issued_by_party_id,
            batch.recorded_by_party_id,
            batch.cancelled_by_party_id,
        )
        if i
    }
    names = {
        str(p.id): p.display_name or p.email or str(p.id)
        for p in db.scalars(select(Party).where(Party.tenant_id == tenant_id, Party.id.in_(ids)))
    }
    bank = db.scalar(
        select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.id == batch.bank_account_id)
    )
    return {"batch": batch, "lines": lines, "names": names, "bank": bank}
