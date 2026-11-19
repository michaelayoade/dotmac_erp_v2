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
   An existing role with login / superuser / bypassrls / createrole / createdb / replication, or any membership, is
   refused. PostgreSQL below 15 is refused.

Revision chain: e2_0028_migration_ledger (B-9) -> this. e2_0030 is reserved for B-14 holdings.
"""

from __future__ import annotations

# All identifiers are closed literals (the table and role names of this package).
# ruff: noqa: S608
import sqlalchemy as sa
from alembic import op

from erp_v2.importer.ar_opening_models import TABLE, WRITER_ROLE

revision = "e2_0029_ar_opening_admissions"
down_revision = "e2_0028_migration_ledger"
branch_labels = None
depends_on = ("bi_0017_open_documents_index",)

_SCHEMA = "mod_billing"
# bi_0009 docstring: INSERT closure.
INSERT_TABLES = (
    "documents",
    "posting_groups",
    "posting_effects",
    "accounting_facts",
    "receivable_position_facts",
    "receivable_position_facts_v2",
    "receivable_exposure_facts_v2",
    "opening_import_receipts",
    "opening_cash_lots",
    "opening_cash_effects",
    "opening_receivable_bases",
    "opening_historical_discharge_claims",
    "cash_source_claims",
    "receivable_source_claims",
    "opening_window_admissions",
    "opening_window_seals",
    # bi_0014: variance ledger written by the import and the seal.
    "opening_variances",
    "opening_variance_events",
    "opening_control_gaps",
    "opening_control_gap_events",
)
# SELECT-only tables the closure reads (REVIEW-R5: derived, not confirmed). The first two are named by bi_0009;
# the rest were found by running the real a12 lane as the restricted role (each a "permission denied" read).
SELECT_ONLY_TABLES = (
    "billing_accounts",
    "opening_windows",
    "confirmed_settlements",
    "rated_obligations",
    "withholding_discharges",
    "non_cash_credit_grants",
    "non_cash_credit_effects",
    "document_events",
    "allocation_effects",
)
KERNEL_TABLES = ("idempotency_records", "outbox_events")  # public schema, SELECT + INSERT


def _grant_lines() -> list[str]:
    lines = [f"GRANT USAGE ON SCHEMA {_SCHEMA} TO {WRITER_ROLE}", f"GRANT USAGE ON SCHEMA public TO {WRITER_ROLE}"]
    for table in (*INSERT_TABLES, *SELECT_ONLY_TABLES):
        lines.append(f"GRANT SELECT ON {_SCHEMA}.{table} TO {WRITER_ROLE}")
    for table in INSERT_TABLES:
        lines.append(f"GRANT INSERT ON {_SCHEMA}.{table} TO {WRITER_ROLE}")
    lines.append(f"GRANT UPDATE (generation) ON {_SCHEMA}.opening_windows TO {WRITER_ROLE}")
    lines.append(f"GRANT UPDATE (id) ON {_SCHEMA}.billing_accounts TO {WRITER_ROLE}")
    for table in KERNEL_TABLES:
        lines.append(f"GRANT SELECT, INSERT ON public.{table} TO {WRITER_ROLE}")
    return lines


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
    op.execute(
        f"""
        DO $$
        DECLARE
            server_major integer := current_setting('server_version_num')::integer / 10000;
            r pg_catalog.pg_roles%ROWTYPE;
        BEGIN
            IF server_major < 15 THEN
                RAISE EXCEPTION 'erp_billing_opening_writer requires PostgreSQL 15 or newer'
                    ' (SET and INHERIT membership options)';
            END IF;
            SELECT * INTO r FROM pg_catalog.pg_roles WHERE rolname = '{WRITER_ROLE}';
            IF NOT FOUND THEN
                CREATE ROLE {WRITER_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            ELSIF r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb
                  OR r.rolreplication THEN
                RAISE EXCEPTION 'existing role {WRITER_ROLE} carries login, superuser, bypassrls, createrole, '
                    'createdb or replication: refusing to adopt it';
            END IF;
            -- never a member (even transitively) of the serving or admin roles, and none of them a member of it
            IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{WRITER_ROLE}') THEN
                IF EXISTS (
                    SELECT 1 FROM pg_catalog.pg_roles g
                    WHERE g.rolname IN ('app_user', 'platform_api', 'app_admin')
                      AND (pg_catalog.pg_has_role('{WRITER_ROLE}', g.oid, 'MEMBER')
                           OR pg_catalog.pg_has_role(g.oid, '{WRITER_ROLE}', 'MEMBER'))
                ) THEN
                    RAISE EXCEPTION '{WRITER_ROLE} is linked to app_user, platform_api or app_admin: refusing';
                END IF;
                -- membership of ANY role (e.g. one granted to the pipeline migrator) is a widening: refuse
                IF EXISTS (
                    SELECT 1 FROM pg_catalog.pg_auth_members m
                    WHERE m.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = '{WRITER_ROLE}')
                ) THEN
                    RAISE EXCEPTION '{WRITER_ROLE} is a member of another role: refusing to adopt it';
                END IF;
            END IF;
        END
        $$
        """
    )
    for line in _grant_lines():
        op.execute(line)
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
