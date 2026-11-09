"""App-owned trusted provider mapping and atomic acceptance provenance (e2 lineage)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk


class BankingProviderBinding(Base, TimestampMixin):
    __tablename__ = "banking_provider_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_banking_provider_bindings_tenant_id"),
        UniqueConstraint(
            "tenant_id", "installation_id", "provider_account_id", name="uq_banking_provider_bindings_source"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["machine_credentials.tenant_id", "machine_credentials.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"], ["mod_banking.bank_accounts.tenant_id", "mod_banking.bank_accounts.id"]
        ),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    credential_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    source_application: Mapped[str] = mapped_column(String(120), nullable=False)
    installation_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    scope_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


class BankingObservationReceipt(Base, TimestampMixin):
    __tablename__ = "banking_observation_receipts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_banking_observation_receipts_tenant_id"),
        UniqueConstraint("tenant_id", "binding_id", "idempotency_key", name="uq_banking_observation_receipts_key"),
        UniqueConstraint(
            "tenant_id", "binding_id", "provider_transaction_id", name="uq_banking_observation_receipts_event"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "binding_id"], ["banking_provider_bindings.tenant_id", "banking_provider_bindings.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "statement_id"], ["mod_banking.bank_statements.tenant_id", "mod_banking.bank_statements.id"]
        ),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    binding_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_transaction_id: Mapped[str] = mapped_column(String(200), nullable=False)
    statement_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


BANKING_TABLES = ("banking_provider_bindings", "banking_observation_receipts")
