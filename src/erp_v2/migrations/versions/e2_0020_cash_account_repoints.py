"""Cash GL re-point requests (two-person re-point of a bank account's cash GL).

Additive: one new table, no change to any existing object. RLS forced; the runtime role may
SELECT, INSERT and UPDATE the decision columns only; no DELETE. The database enforces the
two-person rule and one open request per bank account.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.cash_gl.models import DECISION_UPDATE_COLUMNS, TABLE

revision = "e2_0020_cash_account_repoints"
down_revision = "e2_0019_payroll_payouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_id", sa.Uuid(), nullable=False),
        sa.Column("from_ref", sa.String(240), nullable=False),
        sa.Column("to_account_id", sa.Uuid(), nullable=False),
        sa.Column("to_account_code", sa.String(40), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("evidence_ref", sa.String(240), nullable=False),
        sa.Column("proposed_by_party_id", sa.Uuid(), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(500), nullable=True),
        sa.Column("applied_ref", sa.String(240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bank_account_id"],
            ["mod_banking.bank_accounts.tenant_id", "mod_banking.bank_accounts.id"],
            name=f"fk_{TABLE}_bank_account",
        ),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{TABLE}_tenant_id_id"),
        sa.CheckConstraint("status IN ('proposed', 'approved', 'rejected')", name=f"ck_{TABLE}_status"),
        sa.CheckConstraint("length(btrim(evidence_ref)) > 0", name=f"ck_{TABLE}_evidence"),
        sa.CheckConstraint(
            "status = 'proposed' OR (decided_by_party_id IS NOT NULL AND decided_at IS NOT NULL)",
            name=f"ck_{TABLE}_decided",
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR (decided_by_party_id <> proposed_by_party_id AND applied_ref IS NOT NULL)",
            name=f"ck_{TABLE}_two_person",
        ),
    )
    op.create_index(
        f"uq_{TABLE}_one_proposed",
        TABLE,
        ["tenant_id", "bank_account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'proposed'"),
    )
    op.create_index(f"ix_{TABLE}_account", TABLE, ["tenant_id", "to_account_id"])
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON public.{TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT ON public.{TABLE} TO app_user")
    op.execute(f"GRANT UPDATE ({', '.join(DECISION_UPDATE_COLUMNS)}) ON public.{TABLE} TO app_user")


def downgrade() -> None:
    # The requests are the record of who re-pointed a cash GL: never drop them.
    exists = op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{TABLE})")).scalar()  # noqa: S608
    if exists:
        raise RuntimeError(f"refusing to drop {TABLE}: re-point requests exist")
    op.drop_table(TABLE)
