"""AR opening admission receipts and the restricted opening writer role (rev 2 section 3 / 8).

Two things, both additive:

1. `erp_ar_opening_admissions`: one immutable row per admitted partition (RLS forced; the runtime role gets SELECT
   only; a trigger refuses UPDATE and DELETE even for the owner role; downgrade refuses while any row exists).
2. NOLOGIN role `erp_billing_opening_writer` (SECURITY WIDENING, architect review before the pin PR). a12 binds a
   window to ONE restricted principal and refuses ordinary roles; the operator CLI `SET ROLE`s into it for import
   and seal. Its grants are exactly the closure billing names in the docstrings of bi_0009 / bi_0013 / bi_0014:
   Billing R5 list (billing-step5 BILLING-R1-R4-R5): INSERT only the 20 tables below; no hold or authority-switch
   tables because the ERP uses no holds and passes no authority switch to the seal.
   It is never a member of app_user / platform_api / app_admin, owns nothing, and has no LOGIN. NO membership is
   granted by this migration (not to the migrator, not to anyone): the operator's `SET ROLE` membership is a Michael
   root step for the window's lifetime only (`WITH SET TRUE, INHERIT FALSE`), revoked after the seal.
   An existing role with login / superuser / bypassrls / createrole / createdb / replication, a membership with SET or
   INHERIT held by anyone (an ADMIN-only creator row is allowed), owned objects or default privileges is refused; an
   adopted role has every privilege stripped and the exact R5 set re-granted and asserted. PostgreSQL below 16 is
   refused (see `erp_v2.importer.ar_opening_role`).

Revision chain: e2_0028_migration_ledger (B-9) -> this. e2_0030 is reserved for B-14 holdings.
"""

from __future__ import annotations

# All identifiers are closed literals (the table and role names of this package).
# ruff: noqa: S608
import sqlalchemy as sa
from alembic import op

from erp_v2.importer import ar_opening_role as role_guard
from erp_v2.importer.ar_opening_models import TABLE, WRITER_ROLE
from erp_v2.importer.ar_opening_role import INSERT_TABLES, KERNEL_TABLES, SELECT_ONLY_TABLES, grant_lines  # noqa: F401

revision = "e2_0029_ar_opening_admissions"
down_revision = "e2_0028_migration_ledger"
branch_labels = None
depends_on = ("bi_0017_open_documents_index",)


def _grant_lines() -> list[str]:
    return grant_lines(WRITER_ROLE)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source_account_key", sa.String(255), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("billing_account_id", sa.Uuid(), nullable=False),
        sa.Column("billing_receipt_id", sa.Uuid(), nullable=False),
        sa.Column("posting_group_id", sa.Uuid(), nullable=False),
        sa.Column("window_id", sa.Uuid(), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("manifest_fingerprint", sa.String(64), nullable=False),
        sa.Column("census_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("plan_sha256", sa.String(64), nullable=False),
        sa.Column("applied_by", sa.String(120), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "billing_account_id", name=f"uq_{TABLE}_account"),
        sa.UniqueConstraint("tenant_id", "billing_receipt_id", name=f"uq_{TABLE}_receipt"),
        sa.CheckConstraint("length(btrim(source_account_key)) > 0", name=f"ck_{TABLE}_key"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=f"ck_{TABLE}_currency"),
        sa.CheckConstraint("command_digest ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_command_digest"),
        sa.CheckConstraint("manifest_fingerprint ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_manifest"),
        sa.CheckConstraint("census_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_census"),
        sa.CheckConstraint("snapshot_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_snapshot"),
        sa.CheckConstraint("plan_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_plan"),
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON public.{TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute(
        f"""
        CREATE FUNCTION public.{TABLE}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'AR opening admission receipts are immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_guard BEFORE UPDATE OR DELETE ON public.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{TABLE}_guard()"
    )
    op.execute(f"GRANT SELECT ON public.{TABLE} TO app_user")
    # The restricted opening principal (billing R5). An existing role is verified, never altered or widened.
    conn = op.get_bind()
    role_guard.prepare_role(conn, WRITER_ROLE)
    for line in _grant_lines():
        op.execute(line)
    role_guard.preflight(conn, WRITER_ROLE)  # exact privileges, no SET/INHERIT member, no owned objects
    # NO membership is granted here, to the migration role or anyone else. A standing, inheriting membership of the
    # pipeline migrator would be a permanent widening against R5. The operator's membership is a Michael root step,
    # for the window's lifetime only: GRANT erp_billing_opening_writer TO <operator> WITH SET TRUE, INHERIT FALSE;
    # revoked after the seal together with the writer's INSERT grants (runbook F9).


def downgrade() -> None:
    # The receipts are the evidence of what the opening admitted: never drop them.
    exists = op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{TABLE})")).scalar()
    if exists:
        raise RuntimeError(f"refusing to drop {TABLE}: AR opening admissions exist")
    op.drop_table(TABLE)
    op.execute(f"DROP FUNCTION IF EXISTS public.{TABLE}_guard()")
    # The role is left in place on purpose: billing's window binds it by oid; dropping it would strand a window.
