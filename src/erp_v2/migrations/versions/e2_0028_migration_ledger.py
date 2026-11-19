"""Migration ledger (B-9): exceptions register, sha-bound bridge versions, opening/IAS 8 provenance.

Additive: three new tables, no change to any existing object. RLS forced; the runtime role gets SELECT only (the
operator CLIs write as the migration role). Triggers hold the invariants even against the owner role:

* exceptions: no DELETE; identity columns never change;
* bridge versions: no DELETE; the body is immutable; the ONLY permitted UPDATE is the one-time signature, and a
  signed version is immutable (it must also be ``ready``);
* opening journals: insert-only.

The downgrade refuses while any row exists in any of the three tables.

Revision chain: e2_0027 (held draft decisions) -> this.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.migration_ledger.models import (
    BRIDGE_TABLE,
    EXCEPTIONS_TABLE,
    OPENING_KINDS,
    OPENING_TABLE,
    TERMINAL_OUTCOMES,
)

revision = "e2_0028_migration_ledger"
down_revision = "e2_0027_held_draft_decisions"
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
    op.execute(f"GRANT SELECT ON public.{table} TO app_user")


def _function(name: str, body: str) -> None:
    op.execute(f"CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql AS $$ {body} $$")


def upgrade() -> None:
    outcomes = ", ".join(f"'{o}'" for o in TERMINAL_OUTCOMES)
    kinds = ", ".join(f"'{k}'" for k in OPENING_KINDS)

    op.create_table(
        EXCEPTIONS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("class_code", sa.String(20), nullable=False),
        sa.Column("legacy_key", sa.String(255), nullable=False),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("terminal_outcome", sa.String(30), nullable=True),
        sa.Column("evidence_sha256", sa.String(64), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("source_receipt_sha256", sa.String(64), nullable=False),
        sa.Column("detail", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("reopened_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "class_code", "legacy_key", name=f"uq_{EXCEPTIONS_TABLE}_key"),
        sa.CheckConstraint(
            "length(btrim(class_code)) > 0 AND length(btrim(legacy_key)) > 0", name=f"ck_{EXCEPTIONS_TABLE}_names"
        ),
        sa.CheckConstraint(_SHA.format(col="fingerprint"), name=f"ck_{EXCEPTIONS_TABLE}_fingerprint"),
        sa.CheckConstraint(_SHA.format(col="source_receipt_sha256"), name=f"ck_{EXCEPTIONS_TABLE}_receipt_sha"),
        sa.CheckConstraint("jsonb_typeof(detail) = 'object'", name=f"ck_{EXCEPTIONS_TABLE}_detail"),
        # Never parked: a resolved row carries a terminal outcome, an evidence sha and a time; an open one none.
        sa.CheckConstraint(
            f"(state = 'open' AND terminal_outcome IS NULL AND resolved_at IS NULL) OR "
            f"(state = 'resolved' AND terminal_outcome IN ({outcomes}) AND resolved_at IS NOT NULL "
            "AND evidence_sha256 ~ '^[0-9a-f]{64}$')",
            name=f"ck_{EXCEPTIONS_TABLE}_shape",
        ),
    )
    op.create_index(f"ix_{EXCEPTIONS_TABLE}_state", EXCEPTIONS_TABLE, ["tenant_id", "state"])
    _rls(EXCEPTIONS_TABLE)
    _function(
        f"{EXCEPTIONS_TABLE}_guard",
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'migration exceptions are never deleted' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.class_code <> OLD.class_code "
        "OR NEW.legacy_key <> OLD.legacy_key OR NEW.loaded_at <> OLD.loaded_at THEN "
        "RAISE EXCEPTION 'migration exception identity is immutable' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "RETURN NEW; END",
    )
    op.execute(
        f"CREATE TRIGGER {EXCEPTIONS_TABLE}_guard BEFORE UPDATE OR DELETE ON public.{EXCEPTIONS_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{EXCEPTIONS_TABLE}_guard()"
    )

    op.create_table(
        BRIDGE_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("body", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("itemised_sha256", sa.String(64), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("signed_by", sa.String(120), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "version", name=f"uq_{BRIDGE_TABLE}_version"),
        sa.UniqueConstraint("tenant_id", "sha256", name=f"uq_{BRIDGE_TABLE}_sha"),
        sa.CheckConstraint("version >= 1", name=f"ck_{BRIDGE_TABLE}_version"),
        sa.CheckConstraint(_SHA.format(col="sha256"), name=f"ck_{BRIDGE_TABLE}_sha"),
        sa.CheckConstraint(_SHA.format(col="itemised_sha256"), name=f"ck_{BRIDGE_TABLE}_itemised_sha"),
        sa.CheckConstraint("jsonb_typeof(body) = 'object'", name=f"ck_{BRIDGE_TABLE}_body"),
        sa.CheckConstraint(
            "(signed_by IS NULL AND signed_at IS NULL) OR "
            "(signed_by IS NOT NULL AND length(btrim(signed_by)) > 0 AND signed_at IS NOT NULL AND ready)",
            name=f"ck_{BRIDGE_TABLE}_signature",
        ),
    )
    _rls(BRIDGE_TABLE)
    _function(
        f"{BRIDGE_TABLE}_guard",
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'bridge versions are never deleted' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF OLD.signed_at IS NOT NULL THEN RAISE EXCEPTION 'a signed bridge version is immutable' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.version <> OLD.version "
        "OR NEW.sha256 <> OLD.sha256 OR NEW.body <> OLD.body OR NEW.itemised_sha256 <> OLD.itemised_sha256 "
        "OR NEW.ready <> OLD.ready OR NEW.created_by <> OLD.created_by OR NEW.created_at <> OLD.created_at THEN "
        "RAISE EXCEPTION 'a bridge version body is immutable; only its signature may be set' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "RETURN NEW; END",
    )
    op.execute(
        f"CREATE TRIGGER {BRIDGE_TABLE}_guard BEFORE UPDATE OR DELETE ON public.{BRIDGE_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{BRIDGE_TABLE}_guard()"
    )

    op.create_table(
        OPENING_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("item_ref", sa.String(120), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("account_code", sa.String(30), nullable=True),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("plan_sha256", sa.String(64), nullable=False),
        sa.Column("planned_by", sa.String(120), nullable=False),
        sa.Column("applied_by", sa.String(120), nullable=False),
        sa.Column("dual_control", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "kind", "item_ref", name=f"uq_{OPENING_TABLE}_item"),
        sa.UniqueConstraint("tenant_id", "journal_id", name=f"uq_{OPENING_TABLE}_journal"),
        sa.CheckConstraint(f"kind IN ({kinds})", name=f"ck_{OPENING_TABLE}_kind"),
        sa.CheckConstraint(
            "(kind = 'opening' AND account_code IS NULL) OR (kind = 'ias8' AND account_code IS NOT NULL)",
            name=f"ck_{OPENING_TABLE}_shape",
        ),
        sa.CheckConstraint(_SHA.format(col="source_sha256"), name=f"ck_{OPENING_TABLE}_source_sha"),
        sa.CheckConstraint(_SHA.format(col="evidence_sha256"), name=f"ck_{OPENING_TABLE}_evidence_sha"),
        sa.CheckConstraint(_SHA.format(col="spec_sha256"), name=f"ck_{OPENING_TABLE}_spec_sha"),
        sa.CheckConstraint(_SHA.format(col="plan_sha256"), name=f"ck_{OPENING_TABLE}_plan_sha"),
        sa.CheckConstraint("NOT dual_control OR planned_by <> applied_by", name=f"ck_{OPENING_TABLE}_dual_control"),
    )
    _rls(OPENING_TABLE)
    _function(
        f"{OPENING_TABLE}_guard",
        "BEGIN RAISE EXCEPTION 'opening and IAS 8 provenance is insert-only' "
        "USING ERRCODE = 'integrity_constraint_violation'; END",
    )
    op.execute(
        f"CREATE TRIGGER {OPENING_TABLE}_guard BEFORE UPDATE OR DELETE ON public.{OPENING_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{OPENING_TABLE}_guard()"
    )


def downgrade() -> None:
    # The register, the signed bridge and the opening provenance are cutover evidence: never drop them.
    bind = op.get_bind()
    for table in (EXCEPTIONS_TABLE, BRIDGE_TABLE, OPENING_TABLE):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{table})")).scalar():  # noqa: S608
            raise RuntimeError(f"refusing to drop {table}: migration ledger evidence exists")
    for table in (OPENING_TABLE, BRIDGE_TABLE, EXCEPTIONS_TABLE):
        op.drop_table(table)
        op.execute(f"DROP FUNCTION IF EXISTS public.{table}_guard()")
