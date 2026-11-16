"""Payroll payout batches and lines (B5).

Additive: two new tables, no change to any existing object. Amounts and batch membership are
immutable for the runtime role (UPDATE only on lifecycle columns); no DELETE/TRUNCATE. RLS forced.
The database also enforces distinct preparer/approver/file issuer/recorder.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.payroll_payouts.models import BATCH_UPDATE_COLUMNS, LINE_UPDATE_COLUMNS

revision = "e2_0019_payroll_payouts"
down_revision = "e2_0018_disbursement_receipts"
branch_labels = None
depends_on = None

_B = "erp_payroll_payout_batches"
_L = "erp_payroll_payout_lines"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )


def upgrade() -> None:
    op.create_table(
        _B,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("destination_ref", sa.String(240), nullable=True),
        sa.Column("bank_account_id", sa.Uuid(), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column("total_amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("prepared_by_party_id", sa.Uuid(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_issued_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("file_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_sha256", sa.String(64), nullable=True),
        sa.Column("file_row_count", sa.Integer(), nullable=True),
        sa.Column("recorded_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bank_reference", sa.String(255), nullable=True),
        sa.Column("cancelled_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(500), nullable=True),
        sa.Column("recall_reference", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{_B}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "request_id", name=f"uq_{_B}_request"),
        sa.UniqueConstraint("tenant_id", "number", name=f"uq_{_B}_number"),
        sa.UniqueConstraint("tenant_id", "bank_account_id", "bank_reference", name=f"uq_{_B}_bank_reference"),
        sa.CheckConstraint("kind IN ('net_pay', 'statutory')", name=f"ck_{_B}_kind"),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'file_issued', 'recorded', 'cancelled')", name=f"ck_{_B}_status"
        ),
        sa.CheckConstraint("total_amount > 0 AND line_count > 0", name=f"ck_{_B}_totals"),
        sa.CheckConstraint("kind = 'net_pay' OR file_issued_by_party_id IS NULL", name=f"ck_{_B}_statutory_no_file"),
        sa.CheckConstraint(
            "(approved_by_party_id IS NULL OR approved_by_party_id <> prepared_by_party_id) "
            "AND (file_issued_by_party_id IS NULL OR (file_issued_by_party_id <> prepared_by_party_id "
            "AND file_issued_by_party_id <> approved_by_party_id)) "
            "AND (recorded_by_party_id IS NULL OR (recorded_by_party_id <> prepared_by_party_id "
            "AND recorded_by_party_id <> approved_by_party_id "
            "AND recorded_by_party_id <> file_issued_by_party_id))",
            name=f"ck_{_B}_distinct_people",
        ),
        sa.CheckConstraint(
            "status <> 'recorded' OR (recorded_by_party_id IS NOT NULL AND bank_reference IS NOT NULL)",
            name=f"ck_{_B}_recorded",
        ),
    )
    op.create_index(f"ix_{_B}_run", _B, ["tenant_id", "run_id"])
    op.create_table(
        _L,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("liability_id", sa.Uuid(), nullable=False),
        sa.Column("beneficiary_ref", sa.String(240), nullable=False),
        sa.Column("liability_account_ref", sa.String(240), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("settlement_id", sa.Uuid(), nullable=True),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("journal_number", sa.String(60), nullable=True),
        sa.Column("consequence_fingerprint", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"], [f"{_B}.tenant_id", f"{_B}.id"], ondelete="RESTRICT", name=f"fk_{_L}_batch"
        ),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{_L}_tenant_id_id"),
        sa.CheckConstraint("amount > 0", name=f"ck_{_L}_amount"),
    )
    op.create_index(f"ix_{_L}_batch", _L, ["tenant_id", "batch_id"])
    op.create_index(
        f"uq_{_L}_live_liability",
        _L,
        ["tenant_id", "liability_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    for table, columns in ((_B, BATCH_UPDATE_COLUMNS), (_L, LINE_UPDATE_COLUMNS)):
        _rls(table)
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO app_user")
        op.execute(f"GRANT UPDATE ({', '.join(columns)}) ON public.{table} TO app_user")


def downgrade() -> None:
    # Batches are the record of who approved and recorded money movements: never drop them.
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM public.erp_payroll_payout_batches)")).scalar():
        raise RuntimeError(f"refusing to drop {_B}: payout batches exist")
    op.drop_table(_L)
    op.drop_table(_B)
