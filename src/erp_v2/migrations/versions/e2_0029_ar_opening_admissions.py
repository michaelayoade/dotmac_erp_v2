"""AR opening admission receipts and the restricted opening writer role (rev 2 section 3 / 8).

Two things, both additive:

1. `erp_ar_opening_admissions`: one immutable row per admitted partition (RLS forced; the runtime role gets SELECT
   only; a trigger refuses UPDATE and DELETE even for the owner role; downgrade refuses while any row exists).
2. NOLOGIN role `erp_billing_opening_writer` (SECURITY WIDENING, architect review before the pin PR). a12 binds a
   window to ONE restricted principal and refuses ordinary roles; the operator CLI `SET ROLE`s into it for import
   and seal. Its grants are exactly the closure billing names in the docstrings of bi_0009 / bi_0013 / bi_0014:
   REVIEW-R5: projects-60 has not yet confirmed this list; it is the minimal set derived from those docstrings.
   It is never a member of app_user / platform_api / app_admin, owns nothing, and has no LOGIN. The migration
   role is granted membership so it can `SET ROLE` into it.

Revision chain: e2_0027 (current main head) -> this (e2_0028, B-9, is reserved; the coordinator re-chains).
"""

from __future__ import annotations

# All identifiers are closed literals (the table and role names of this package).
# ruff: noqa: S608
import sqlalchemy as sa
from alembic import op

from erp_v2.importer.ar_opening_models import TABLE, WRITER_ROLE

revision = "e2_0029_ar_opening_admissions"
down_revision = "e2_0027_held_draft_decisions"
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
    # The restricted opening principal. Idempotent: an existing role is verified, never altered or widened.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{WRITER_ROLE}') THEN
                CREATE ROLE {WRITER_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            END IF;
        END
        $$
        """
    )
    for line in _grant_lines():
        op.execute(line)
    # The migration role (the CLI's session) must be able to SET ROLE into the writer for import and seal.
    # A role created earlier by a DIFFERENT migration role (shared test clusters) cannot be re-granted here; then the
    # operator's session simply cannot SET ROLE into it and the CLI fails closed (nothing is widened silently).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT pg_catalog.pg_has_role(current_user, '{WRITER_ROLE}', 'SET') THEN
                BEGIN
                    EXECUTE 'GRANT {WRITER_ROLE} TO ' || quote_ident(current_user);
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'migration role cannot be granted {WRITER_ROLE}: grant membership out of band';
                END;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    # The receipts are the evidence of what the opening admitted: never drop them.
    exists = op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{TABLE})")).scalar()
    if exists:
        raise RuntimeError(f"refusing to drop {TABLE}: AR opening admissions exist")
    op.drop_table(TABLE)
    op.execute(f"DROP FUNCTION IF EXISTS public.{TABLE}_guard()")
    # The role is left in place on purpose: billing's window binds it by oid; dropping it would strand a window.
