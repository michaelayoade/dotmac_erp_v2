# ruff: noqa: S608  (the SQL interpolates module-constant table names only, never input)
"""B-15 rev 2: cash-basis output-VAT positions and movements for the billing -> GL mapper.

Additive: two new tables. FORCE RLS with the tenant policy. Both are append-only (no UPDATE, no DELETE, enforced by
triggers even against the owner role).

W6 (explicit widening, approved by Michael 2026-11-19; extends W4): ``app_user`` gets SELECT and INSERT only. The
opening loader (``python -m erp_v2.billing_gl vat-opening``) writes as the migration role.

Backstop trigger on movements: the position's deferral balance R_k must stay within 0..D_k and the document's open
amount must stay >= 0 (``vat_invariant``), whatever wrote the row. The cross-position rule (open = 0 implies every
R_k = 0) is asserted by the mapper in the same transaction before it posts.

The downgrade refuses while rows exist.

Revision chain: e2_0032_ar_producer (B-16) -> this.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.billing_gl.models import MOVE_KINDS, VAT_MOVEMENTS_TABLE, VAT_POSITIONS_TABLE

revision = "e2_0033_billing_gl_vat"
down_revision = "e2_0032_ar_producer"
branch_labels = None
depends_on = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT ON public.{table} TO app_user")  # W6: no UPDATE, no DELETE


def upgrade() -> None:
    kinds = ", ".join(f"'{k}'" for k in MOVE_KINDS)
    op.create_table(
        VAT_POSITIONS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("treatment_code", sa.String(80), nullable=False),
        sa.Column("deferral_account_id", sa.Uuid(), nullable=True),
        sa.Column("current_account_id", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("total", sa.Numeric(20, 6), nullable=False),
        sa.Column("deferred", sa.Numeric(20, 6), nullable=False),
        sa.Column("open_initial", sa.Numeric(20, 6), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "document_id", "treatment_code", name=f"uq_{VAT_POSITIONS_TABLE}_key"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{VAT_POSITIONS_TABLE}_tenant_id"),
        sa.CheckConstraint("origin IN ('native', 'opening')", name=f"ck_{VAT_POSITIONS_TABLE}_origin"),
        sa.CheckConstraint(
            "total > 0 AND deferred >= 0 AND deferred <= total AND open_initial >= 0 AND open_initial <= total",
            name=f"ck_{VAT_POSITIONS_TABLE}_amounts",
        ),
        sa.CheckConstraint(
            "deferred = 0 OR (deferral_account_id IS NOT NULL AND current_account_id IS NOT NULL)",
            name=f"ck_{VAT_POSITIONS_TABLE}_accounts",
        ),
    )
    _rls(VAT_POSITIONS_TABLE)
    op.execute(
        f"CREATE FUNCTION public.{VAT_POSITIONS_TABLE}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'INSERT' THEN "
        # The runtime role may only create native positions; an opening position is the loader's (migration role).
        "IF current_user = 'app_user' AND NEW.origin <> 'native' THEN "
        "RAISE EXCEPTION 'opening VAT positions are loaded by the migration role only' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; RETURN NEW; END IF; "
        "RAISE EXCEPTION 'VAT positions are append-only' USING ERRCODE = 'integrity_constraint_violation'; END $$"
    )
    op.execute(
        f"CREATE TRIGGER {VAT_POSITIONS_TABLE}_guard BEFORE INSERT OR UPDATE OR DELETE ON public.{VAT_POSITIONS_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{VAT_POSITIONS_TABLE}_guard()"
    )

    op.create_table(
        VAT_MOVEMENTS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("deferral_delta", sa.Numeric(20, 6), nullable=False),
        sa.Column("current_delta", sa.Numeric(20, 6), nullable=False),
        sa.Column("open_delta", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("carrier", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("allocation_ref", sa.String(80), nullable=False, server_default=""),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "position_id"], [f"{VAT_POSITIONS_TABLE}.tenant_id", f"{VAT_POSITIONS_TABLE}.id"]
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "fact_id",
            "fact_version",
            "position_id",
            "kind",
            "allocation_ref",
            name=f"uq_{VAT_MOVEMENTS_TABLE}_key",
        ),
        sa.CheckConstraint(f"kind IN ({kinds})", name=f"ck_{VAT_MOVEMENTS_TABLE}_kind"),
        sa.CheckConstraint("open_delta = 0 OR carrier", name=f"ck_{VAT_MOVEMENTS_TABLE}_carrier"),
    )
    op.create_index(f"ix_{VAT_MOVEMENTS_TABLE}_position", VAT_MOVEMENTS_TABLE, ["tenant_id", "position_id"])
    op.create_index(f"ix_{VAT_MOVEMENTS_TABLE}_fact", VAT_MOVEMENTS_TABLE, ["tenant_id", "fact_id"])
    _rls(VAT_MOVEMENTS_TABLE)
    op.execute(
        f"CREATE FUNCTION public.{VAT_MOVEMENTS_TABLE}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "DECLARE d numeric; r numeric; o numeric; "
        "BEGIN "
        "IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'VAT movements are append-only' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF current_user = 'app_user' AND NEW.fact_version = 0 THEN "
        "RAISE EXCEPTION 'opening VAT movements are loaded by the migration role only' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        f"SELECT deferred, open_initial INTO d, o FROM public.{VAT_POSITIONS_TABLE} "
        "WHERE tenant_id = NEW.tenant_id AND id = NEW.position_id; "
        f"SELECT COALESCE(SUM(deferral_delta), 0) INTO r FROM public.{VAT_MOVEMENTS_TABLE} "
        "WHERE tenant_id = NEW.tenant_id AND position_id = NEW.position_id; "
        "IF r + NEW.deferral_delta < 0 OR r + NEW.deferral_delta > d THEN "
        "RAISE EXCEPTION 'vat_invariant: deferral balance out of range' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.carrier THEN "
        f"SELECT o + COALESCE(SUM(open_delta), 0) INTO o FROM public.{VAT_MOVEMENTS_TABLE} "
        "WHERE tenant_id = NEW.tenant_id AND position_id = NEW.position_id AND carrier; "
        "IF o + NEW.open_delta < 0 THEN RAISE EXCEPTION 'vat_invariant: open amount below zero' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; END IF; "
        "RETURN NEW; END $$"
    )
    op.execute(
        f"CREATE TRIGGER {VAT_MOVEMENTS_TABLE}_guard BEFORE INSERT OR UPDATE OR DELETE ON public.{VAT_MOVEMENTS_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{VAT_MOVEMENTS_TABLE}_guard()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (VAT_MOVEMENTS_TABLE, VAT_POSITIONS_TABLE):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{table})")).scalar():
            raise RuntimeError(f"refusing to drop {table}: billing VAT evidence exists")
    for table in (VAT_MOVEMENTS_TABLE, VAT_POSITIONS_TABLE):
        op.drop_table(table)
        op.execute(f"DROP FUNCTION IF EXISTS public.{table}_guard()")
