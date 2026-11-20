"""ERP-owned customer finance row (e2_0035): what dotmac-customers does not carry.

One row per customer account, keyed by the plain customer account id (no foreign key into ``mod_customers``: this
lineage takes no cross-lineage migration dependency; the service resolves the account in the tenant through
``customers.get_account``). Never financial identity: billing keys off the customer account id. No JSON, no free
metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import CheckConstraint, DateTime, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

TABLE: Final = "erp_customer_finance"

MAX_TRADING_NAME: Final = 200
MAX_TIN: Final = 50
MAX_TERMS_CODE: Final = 40
MAX_CONTACT_NAME: Final = 200
MAX_EMAIL: Final = 254
MAX_PHONE: Final = 50
MAX_ADDRESS: Final = 500

# The columns the runtime role may UPDATE (W10); everything else is immutable by trigger.
EDITABLE_COLUMNS: Final = (
    "trading_name",
    "tax_identification_number",
    "payment_terms_code",
    "contact_name",
    "contact_email",
    "contact_phone",
    "billing_address",
    "updated_at",
    "updated_by",
)


class CustomerFinance(Base):
    __tablename__ = TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "customer_account_id", name=f"uq_{TABLE}_customer"),
        CheckConstraint(f"char_length(trading_name) <= {MAX_TRADING_NAME}", name=f"ck_{TABLE}_trading_name"),
        CheckConstraint(f"char_length(tax_identification_number) <= {MAX_TIN}", name=f"ck_{TABLE}_tin"),
        CheckConstraint(f"char_length(payment_terms_code) <= {MAX_TERMS_CODE}", name=f"ck_{TABLE}_terms"),
        CheckConstraint(f"char_length(contact_name) <= {MAX_CONTACT_NAME}", name=f"ck_{TABLE}_contact_name"),
        CheckConstraint(f"char_length(contact_email) <= {MAX_EMAIL}", name=f"ck_{TABLE}_contact_email"),
        CheckConstraint(f"char_length(contact_phone) <= {MAX_PHONE}", name=f"ck_{TABLE}_contact_phone"),
        CheckConstraint(f"char_length(billing_address) <= {MAX_ADDRESS}", name=f"ck_{TABLE}_address"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    customer_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    trading_name: Mapped[str | None] = mapped_column(String(MAX_TRADING_NAME), nullable=True)
    tax_identification_number: Mapped[str | None] = mapped_column(String(MAX_TIN), nullable=True)
    payment_terms_code: Mapped[str | None] = mapped_column(String(MAX_TERMS_CODE), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(MAX_CONTACT_NAME), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(MAX_EMAIL), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(MAX_PHONE), nullable=True)
    billing_address: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_by: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
