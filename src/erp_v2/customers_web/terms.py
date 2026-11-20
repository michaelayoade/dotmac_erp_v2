"""Read side for B-16: the payment terms code recorded on a customer (a key of ``ar.payment_terms``) or None.

The invoice form pre-selects it and falls back to ``ar.default_payment_terms_code`` when this is None or no longer a
key. A draft stores its own code (B-16 finding 3), so a later customer edit never changes a draft or an issued invoice.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.customers_web.models import CustomerFinance


def code_for(db: Session, tenant_id: UUID, customer_account_id: UUID) -> str | None:
    return db.scalar(
        select(CustomerFinance.payment_terms_code).where(
            CustomerFinance.tenant_id == tenant_id, CustomerFinance.customer_account_id == customer_account_id
        )
    )
