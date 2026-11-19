"""B-15 live billing -> GL mapper: per-fact disposition rows and the fact-window ledger.

Additive: two new tables, no change to any existing object. FORCE RLS with the tenant policy.

W4 (explicit widening, approved by Michael 2026-11-19): the mapper runs as the runtime role, so ``app_user`` gets
SELECT, INSERT and a column-limited UPDATE on both tables, and no DELETE. These are the first runtime-writable
migration-family tables; triggers hold the invariants even against the owner role:

* postings: no DELETE; identity and ``fact_digest`` immutable (a digest change raises
  ``billing_fact_digest_changed``, an integrity alarm); POSTED, SKIP and NO_GL are terminal; BLOCKED and WAITING move
  only to a terminal state or to each other; REFUSED moves only to POSTED (after a reviewed dispatch change);
* windows: no DELETE; the window bounds never change.

The downgrade refuses while any row exists.

Revision chain: e2_0030_migration_holdings (B-14) -> this.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.billing_gl.models import DISPOSITIONS, POSTINGS_TABLE, WINDOWS_TABLE

revision = "e2_0031_billing_fact_postings"
down_revision = "e2_0030_migration_holdings"
branch_labels = None
depends_on = None

_SHA = "{col} ~ '^[0-9a-f]{{64}}$'"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )


def _guard(table: str, body: str) -> None:
    op.execute(f"CREATE FUNCTION public.{table}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ {body} $$")
    op.execute(
        f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON public.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{table}_guard()"
    )


def upgrade() -> None:
    states = ", ".join(f"'{s}'" for s in DISPOSITIONS)
    op.create_table(
        POSTINGS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version", sa.Integer(), nullable=False),
        sa.Column("effect_kind", sa.String(60), nullable=False),
        sa.Column("fact_digest", sa.String(64), nullable=False),
        sa.Column("billing_account_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disposition", sa.String(10), nullable=False),
        sa.Column("reason", sa.String(80), nullable=True),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("map_digest", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "fact_id", "fact_version", name=f"uq_{POSTINGS_TABLE}_fact"),
        sa.CheckConstraint(f"disposition IN ({states})", name=f"ck_{POSTINGS_TABLE}_disposition"),
        sa.CheckConstraint("fact_version >= 1 AND attempts >= 0", name=f"ck_{POSTINGS_TABLE}_counts"),
        sa.CheckConstraint(_SHA.format(col="fact_digest"), name=f"ck_{POSTINGS_TABLE}_digest"),
        sa.CheckConstraint(
            "map_digest IS NULL OR map_digest ~ '^[0-9a-f]{64}$'", name=f"ck_{POSTINGS_TABLE}_map_digest"
        ),
        sa.CheckConstraint("(disposition = 'POSTED') = (journal_id IS NOT NULL)", name=f"ck_{POSTINGS_TABLE}_journal"),
    )
    op.create_index(f"ix_{POSTINGS_TABLE}_open", POSTINGS_TABLE, ["tenant_id", "disposition", "occurred_at"])
    op.create_index(f"ix_{POSTINGS_TABLE}_account", POSTINGS_TABLE, ["tenant_id", "billing_account_id", "occurred_at"])
    _rls(POSTINGS_TABLE)
    _guard(
        POSTINGS_TABLE,
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'billing fact postings are never deleted' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.fact_digest <> OLD.fact_digest THEN RAISE EXCEPTION 'billing_fact_digest_changed' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.fact_id <> OLD.fact_id "
        "OR NEW.fact_version <> OLD.fact_version OR NEW.effect_kind <> OLD.effect_kind "
        "OR NEW.billing_account_id <> OLD.billing_account_id OR NEW.occurred_at <> OLD.occurred_at "
        "OR NEW.first_seen_at <> OLD.first_seen_at "
        "OR (OLD.currency IS NOT NULL AND NEW.currency IS DISTINCT FROM OLD.currency) THEN "
        "RAISE EXCEPTION 'billing fact identity is immutable' USING ERRCODE = 'integrity_constraint_violation'; "
        "END IF; "
        "IF OLD.disposition IN ('POSTED', 'SKIP', 'NO_GL') THEN "
        "RAISE EXCEPTION 'a terminal billing fact disposition is immutable' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF OLD.disposition IN ('BLOCKED', 'WAITING') AND NEW.disposition = 'REFUSED' THEN "
        "RAISE EXCEPTION 'a pending billing fact cannot become REFUSED' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF OLD.disposition = 'REFUSED' AND NEW.disposition <> 'POSTED' THEN "
        "RAISE EXCEPTION 'a REFUSED billing fact may only move to POSTED' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "RETURN NEW; END",
    )
    op.execute(f"GRANT SELECT ON public.{POSTINGS_TABLE} TO app_user")

    op.create_table(
        WINDOWS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rechecked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "occurred_from", name=f"uq_{WINDOWS_TABLE}_from"),
        sa.CheckConstraint("occurred_from < occurred_to AND count >= 0", name=f"ck_{WINDOWS_TABLE}_shape"),
        sa.CheckConstraint(_SHA.format(col="digest"), name=f"ck_{WINDOWS_TABLE}_digest"),
    )
    _rls(WINDOWS_TABLE)
    _guard(
        WINDOWS_TABLE,
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'billing fact windows are never deleted' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.occurred_from <> OLD.occurred_from "
        "OR NEW.occurred_to <> OLD.occurred_to OR NEW.created_at <> OLD.created_at THEN "
        "RAISE EXCEPTION 'billing fact window bounds are immutable' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "RETURN NEW; END",
    )
    op.execute(f"GRANT SELECT ON public.{WINDOWS_TABLE} TO app_user")


def downgrade() -> None:
    bind = op.get_bind()
    for table in (POSTINGS_TABLE, WINDOWS_TABLE):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{table})")).scalar():  # noqa: S608
            raise RuntimeError(f"refusing to drop {table}: billing mapper evidence exists")
    for table in (WINDOWS_TABLE, POSTINGS_TABLE):
        op.drop_table(table)
        op.execute(f"DROP FUNCTION IF EXISTS public.{table}_guard()")
