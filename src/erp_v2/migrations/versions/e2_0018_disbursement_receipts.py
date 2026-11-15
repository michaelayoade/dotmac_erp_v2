"""Disbursement file receipts (B3, shared with B5): sha256 + row count + payee instruction id.

Additive: one new table, no change to any existing object. Bank-file bytes are never stored;
the receipt records only that a file with this sha256 and row count was generated for a document
against a specific payee bank instruction, so recording can refuse when that destination changed.

Security posture: RLS forced with the tenant isolation policy; the runtime role may SELECT and
INSERT only (no UPDATE, DELETE or TRUNCATE): receipts are an append-only trail.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0018_disbursement_receipts"
down_revision = "e2_0017_bank_reconciliation"
branch_labels = None
depends_on = None

_TABLE = "erp_disbursement_file_receipts"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_kind", sa.String(32), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("payee_instruction_id", sa.Uuid(), nullable=False),
        sa.Column("file_format", sa.String(32), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("generated_by_party_id", sa.Uuid(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_erp_disbursement_file_receipts_tenant_id_id"),
        # One file per document: a second file for the same payment/payout is never issued.
        sa.UniqueConstraint(
            "tenant_id", "document_kind", "document_id", name="uq_erp_disbursement_file_receipts_document"
        ),
        sa.CheckConstraint(
            "document_kind IN ('supplier_payment', 'payroll_payout')", name="ck_erp_disbursement_file_receipts_kind"
        ),
        sa.CheckConstraint("row_count > 0", name="ck_erp_disbursement_file_receipts_rows"),
        sa.CheckConstraint("char_length(file_sha256) = 64", name="ck_erp_disbursement_file_receipts_sha"),
    )
    op.create_index("ix_erp_disbursement_file_receipts_document", _TABLE, ["tenant_id", "document_kind", "document_id"])
    op.execute(f"ALTER TABLE public.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_TABLE}_tenant_isolation ON public.{_TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT ON public.{_TABLE} TO app_user")


def downgrade() -> None:
    op.drop_table(_TABLE)
