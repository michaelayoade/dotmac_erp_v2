"""Read-only evidence candidates; this is not a financial matching policy."""

from uuid import UUID

from dotmac_banking.models import BankAccount, BankStatement, BankStatementLine
from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from erp_v2.banking.models import BankingObservationReceipt

PAGE_SIZE = 50
MAX_PAGE = 100


def review(db: Session, *, tenant_id: UUID, account_id: UUID | None = None, page: int = 1) -> dict[str, object]:
    """Compare receipt-backed lines with non-receipt statement lines, exactly.

    References are compared as stored, case sensitively; blanks never constitute
    identity evidence. Equal date/amount is only weak evidence even when both
    references are present and different. No decisions or balances are changed.
    """
    if not 1 <= page <= MAX_PAGE:
        raise HTTPException(422, "Review page is outside the supported range")
    observed, other = aliased(BankStatementLine), aliased(BankStatementLine)
    accepted, imported = aliased(BankStatement), aliased(BankStatement)
    receipt = BankingObservationReceipt
    strong = and_(
        func.trim(observed.external_ref, " \t\r\n\v\f") != "",
        func.trim(other.external_ref, " \t\r\n\v\f") != "",
        observed.external_ref == other.external_ref,
    )
    # Suppress autoflush even when a caller already has pending ORM objects.
    with db.no_autoflush:
        account_query = select(BankAccount).where(BankAccount.tenant_id == tenant_id)
        if account_id is not None:
            account_query = account_query.where(BankAccount.id == account_id)
            if db.scalar(account_query) is None:
                raise HTTPException(404, "Bank account not found")
        accounts = db.scalars(account_query.order_by(BankAccount.account_code).limit(101)).all()
        query = (
            select(receipt, BankAccount, accepted, observed, imported, other, strong.label("strong_reference"))
            .select_from(receipt)
            .join(accepted, and_(accepted.id == receipt.statement_id, accepted.tenant_id == tenant_id))
            .join(BankAccount, and_(BankAccount.id == accepted.account_id, BankAccount.tenant_id == tenant_id))
            .join(observed, and_(observed.statement_id == accepted.id, observed.tenant_id == tenant_id))
            .join(imported, and_(imported.account_id == accepted.account_id, imported.tenant_id == tenant_id))
            .join(other, and_(other.statement_id == imported.id, other.tenant_id == tenant_id))
            .where(
                receipt.tenant_id == tenant_id,
                accepted.id != imported.id,
                accepted.currency_code == imported.currency_code,
                accepted.minor_units == imported.minor_units,
                accepted.currency_code == BankAccount.currency_code,
                accepted.minor_units == BankAccount.minor_units,
                observed.amount == other.amount,
                observed.direction == other.direction,
                or_(strong, observed.transaction_date == other.transaction_date),
                ~exists()
                .where(
                    BankingObservationReceipt.tenant_id == tenant_id,
                    BankingObservationReceipt.statement_id == imported.id,
                )
                .correlate(imported),
            )
            .order_by(receipt.id, observed.id, imported.id, other.id)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE + 1)
        )
        if account_id is not None:
            query = query.where(BankAccount.id == account_id)
        rows = db.execute(query).all()
    return {
        "accounts": accounts[:100],
        "accounts_truncated": len(accounts) > 100,
        "account_id": account_id,
        "page": page,
        "candidates": rows[:PAGE_SIZE],
        "has_more": len(rows) > PAGE_SIZE,
        "max_page": MAX_PAGE,
    }
