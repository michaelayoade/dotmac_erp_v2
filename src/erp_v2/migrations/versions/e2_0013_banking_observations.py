"""Trusted banking bindings and atomic observation receipts.

Frozen ERP-owned shapes. Provider transport remains unmounted pending
trusted destination and account/overlap enrolment; no Mono secrets live here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0013_banking_observations"
down_revision = "e2_0012_academy_sync"
branch_labels = None
depends_on = ("bk_0001_banking",)

_TABLES = ("banking_provider_bindings", "banking_observation_receipts")


def _common():
    return (
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )


def upgrade() -> None:
    op.create_table(
        "banking_provider_bindings",
        *_common(),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("source_application", sa.String(120), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("provider_account_id", sa.String(160), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("scope_kind", sa.String(80), nullable=False),
        sa.Column("scope_ref", sa.String(240), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_banking_provider_bindings_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "installation_id", "provider_account_id", name="uq_banking_provider_bindings_source"
        ),
        sa.ForeignKeyConstraint(["credential_id"], ["machine_credentials.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"], ["mod_banking.bank_accounts.tenant_id", "mod_banking.bank_accounts.id"]
        ),
    )
    op.create_table(
        "banking_observation_receipts",
        *_common(),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("provider_transaction_id", sa.String(200), nullable=False),
        sa.Column("statement_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.String(200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_banking_observation_receipts_tenant_id"),
        sa.UniqueConstraint("tenant_id", "binding_id", "idempotency_key", name="uq_banking_observation_receipts_key"),
        sa.UniqueConstraint(
            "tenant_id", "binding_id", "provider_transaction_id", name="uq_banking_observation_receipts_event"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "binding_id"], ["banking_provider_bindings.tenant_id", "banking_provider_bindings.id"]
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "statement_id"], ["mod_banking.bank_statements.tenant_id", "mod_banking.bank_statements.id"]
        ),
    )
    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
            "USING (tenant_id = public.app_current_tenant_id()) "
            "WITH CHECK (tenant_id = public.app_current_tenant_id())"
        )

    # The runtime can lock an existing trusted binding but cannot create it.
    # PostgreSQL SELECT FOR UPDATE requires UPDATE privilege. Accepted
    # observation receipts are append-only through the service contract.
    op.execute("GRANT SELECT, UPDATE ON public.banking_provider_bindings TO app_user")
    op.execute("GRANT SELECT, INSERT ON public.banking_observation_receipts TO app_user")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
