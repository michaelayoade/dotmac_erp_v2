"""Tenant-scoped banking workspace read model."""

from uuid import UUID

from dotmac_banking.models import BankAccount, BankStatement
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.banking.models import BankingObservationReceipt, BankingProviderBinding


def workspace(db: Session, *, tenant_id: UUID) -> dict[str, object]:
    return {
        "accounts": db.scalars(
            select(BankAccount).where(BankAccount.tenant_id == tenant_id).order_by(BankAccount.account_code).limit(100)
        ).all(),
        "statements": db.scalars(
            select(BankStatement)
            .where(BankStatement.tenant_id == tenant_id)
            .order_by(BankStatement.imported_at.desc())
            .limit(50)
        ).all(),
        "bindings": db.scalars(
            select(BankingProviderBinding)
            .where(BankingProviderBinding.tenant_id == tenant_id)
            .order_by(BankingProviderBinding.id)
            .limit(100)
        ).all(),
        "receipts": db.scalars(
            select(BankingObservationReceipt)
            .where(BankingObservationReceipt.tenant_id == tenant_id)
            .order_by(BankingObservationReceipt.created_at.desc())
            .limit(50)
        ).all(),
    }
