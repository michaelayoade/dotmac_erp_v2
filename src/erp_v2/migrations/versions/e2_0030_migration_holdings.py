# ruff: noqa: E501, S608
"""Migration holdings (B-14): the append-only ledger of control-difference movements onto holding accounts.

Additive: one new table. RLS forced; the runtime role gets SELECT only (the operator CLIs write as the migration
role, the same model as e2_0028). The trigger holds the invariants even against the owner role:

* UPDATE and DELETE always raise;
* a ``release`` or ``writeoff`` INSERT needs an existing ``reclass`` row for the same exception with the same
  control code, currency and holding account, the OPPOSITE sign, and the new outstanding (sum of amount over the
  exception) must keep the reclass's sign or reach zero (no over-release, no flip).

Amount is signed; positive is a DEBIT on the holding account. The downgrade refuses while any row exists.

Revision chain: e2_0029_ar_opening_admissions (#72) -> this.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.holdings.models import HOLDING_CODES, HOLDING_KINDS, HOLDINGS_TABLE
from erp_v2.migration_ledger.models import EXCEPTIONS_TABLE

revision = "e2_0030_migration_holdings"
down_revision = "e2_0029_ar_opening_admissions"
branch_labels = None
depends_on = None

_SHA = "COALESCE({col} ~ '^[0-9a-f]{{64}}$', false)"


def upgrade() -> None:
    codes = ", ".join(f"'{c}'" for c in HOLDING_CODES)
    kinds = ", ".join(f"'{k}'" for k in HOLDING_KINDS)
    t = HOLDINGS_TABLE
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("exception_id", sa.Uuid(), nullable=False),
        sa.Column("control_code", sa.String(30), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("holding_account_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("journal_source_document_id", sa.String(120), nullable=False),
        sa.Column("plan_sha256", sa.String(64), nullable=True),
        sa.Column("owner_receipt_sha256", sa.String(64), nullable=True),
        sa.Column("evidence_sha256", sa.String(64), nullable=True),
        sa.Column("planned_by", sa.String(120), nullable=False),
        sa.Column("applied_by", sa.String(120), nullable=False),
        sa.Column("dual_control", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["exception_id"], [f"{EXCEPTIONS_TABLE}.id"]),
        sa.UniqueConstraint("tenant_id", "journal_id", "exception_id", name=f"uq_{t}_journal"),
        sa.CheckConstraint(f"control_code IN ({codes})", name=f"ck_{t}_code"),
        sa.CheckConstraint(f"kind IN ({kinds})", name=f"ck_{t}_kind"),
        sa.CheckConstraint("amount <> 0", name=f"ck_{t}_amount"),
        sa.CheckConstraint("length(currency) = 3", name=f"ck_{t}_currency"),
        sa.CheckConstraint(f"kind <> 'reclass' OR ({_SHA.format(col='plan_sha256')})", name=f"ck_{t}_plan_sha"),
        sa.CheckConstraint(
            f"(kind = 'release') = {_SHA.format(col='owner_receipt_sha256')}",
            name=f"ck_{t}_owner_receipt",
        ),
        sa.CheckConstraint(
            f"kind <> 'writeoff' OR ({_SHA.format(col='evidence_sha256')})", name=f"ck_{t}_writeoff_evidence"
        ),
        sa.CheckConstraint("NOT dual_control OR planned_by <> applied_by", name=f"ck_{t}_dual_control"),
    )
    op.create_index(
        f"uq_{t}_reclass_once",
        t,
        ["tenant_id", "exception_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'reclass'"),
    )
    op.create_index(f"ix_{t}_exception", t, ["tenant_id", "exception_id"])
    op.create_index(f"ix_{t}_account", t, ["tenant_id", "holding_account_id"])
    op.execute(f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{t} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {t}_tenant_isolation ON public.{t} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute(f"GRANT SELECT ON public.{t} TO app_user")
    op.execute(
        f"CREATE FUNCTION public.{t}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "DECLARE r RECORD; outstanding numeric; "
        "BEGIN "
        "IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'migration holdings are append-only' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.kind = 'reclass' THEN RETURN NEW; END IF; "
        # serialise concurrent releases of one item on the exception row (a no-op for the already-locked CLI path)
        f"PERFORM 1 FROM public.{EXCEPTIONS_TABLE} WHERE id = NEW.exception_id FOR UPDATE; "
        f"SELECT * INTO r FROM public.{t} WHERE tenant_id = NEW.tenant_id AND exception_id = NEW.exception_id "
        "AND kind = 'reclass'; "
        "IF NOT FOUND THEN RAISE EXCEPTION 'a release or writeoff needs the exception''s reclass' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF r.control_code <> NEW.control_code OR r.currency <> NEW.currency "
        "OR r.holding_account_id <> NEW.holding_account_id THEN "
        "RAISE EXCEPTION 'a release or writeoff must match the reclass control code, currency and holding account' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF sign(NEW.amount) = sign(r.amount) THEN RAISE EXCEPTION 'a release or writeoff must be opposite in sign to the reclass' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        f"SELECT coalesce(sum(amount), 0) INTO outstanding FROM public.{t} "
        "WHERE tenant_id = NEW.tenant_id AND exception_id = NEW.exception_id; "
        "outstanding := outstanding + NEW.amount; "
        "IF outstanding <> 0 AND sign(outstanding) <> sign(r.amount) THEN "
        "RAISE EXCEPTION 'a release or writeoff exceeds the outstanding holding amount' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "RETURN NEW; END $$"
    )
    op.execute(
        f"CREATE TRIGGER {t}_guard BEFORE INSERT OR UPDATE OR DELETE ON public.{t} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{t}_guard()"
    )


def downgrade() -> None:
    # Holding movements are cutover evidence: never drop them.
    bind = op.get_bind()
    t = HOLDINGS_TABLE
    if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{t})")).scalar():
        raise RuntimeError(f"refusing to drop {t}: migration holding evidence exists")
    op.drop_table(t)
    op.execute(f"DROP FUNCTION IF EXISTS public.{t}_guard()")
