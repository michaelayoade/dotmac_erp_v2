"""Staff loan entries: the per-employee subledger behind the 1410 Staff Loan control account.

Additive: one new table, no change to any existing object. RLS forced; the runtime role may SELECT,
INSERT and UPDATE the lifecycle columns only; no DELETE. Amounts, kind and membership are immutable
after insert (the database refuses to change them even for the owner role).

Revision chain: e2_0021 -> e2_0022 (PR-4, dual-control policy) -> this revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.staff_loans.models import KINDS, STATES, TABLE, UPDATE_COLUMNS

revision = "e2_0023_staff_loan_entries"
down_revision = "e2_0022_dual_control_policy"
branch_labels = None
depends_on = None

_IMMUTABLE = (
    "id",
    "tenant_id",
    "request_id",
    "employee_ref",
    "kind",
    "amount",
    "evidence_ref",
    "description",
    "value_date",
    "bank_account_id",
    "offset_account_id",
    "prepared_by_party_id",
    "prepared_at",
    "dual_control",
)


def upgrade() -> None:
    kinds = ", ".join(f"'{k}'" for k in KINDS)
    states = ", ".join(f"'{s}'" for s in STATES)
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("employee_ref", sa.String(240), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("evidence_ref", sa.String(240), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column("bank_account_id", sa.Uuid(), nullable=True),
        sa.Column("offset_account_id", sa.Uuid(), nullable=True),
        sa.Column("bank_reference", sa.String(255), nullable=True),
        sa.Column("journal_id", sa.Uuid(), nullable=True),
        sa.Column("dual_control", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("prepared_by_party_id", sa.Uuid(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bank_account_id"],
            ["mod_banking.bank_accounts.tenant_id", "mod_banking.bank_accounts.id"],
            name=f"fk_{TABLE}_bank_account",
        ),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{TABLE}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "request_id", name=f"uq_{TABLE}_request"),
        sa.CheckConstraint(f"kind IN ({kinds})", name=f"ck_{TABLE}_kind"),
        sa.CheckConstraint(f"state IN ({states})", name=f"ck_{TABLE}_state"),
        sa.CheckConstraint("amount <> 0", name=f"ck_{TABLE}_amount"),
        sa.CheckConstraint("length(btrim(evidence_ref)) > 0", name=f"ck_{TABLE}_evidence"),
        sa.CheckConstraint(
            "(kind = 'control_difference' AND employee_ref IS NULL) OR "
            "(kind <> 'control_difference' AND employee_ref IS NOT NULL AND length(btrim(employee_ref)) > 0)",
            name=f"ck_{TABLE}_employee",
        ),
        sa.CheckConstraint(
            "(kind = 'opening' AND amount > 0 AND bank_account_id IS NULL AND offset_account_id IS NULL) OR "
            "(kind = 'advance' AND amount > 0 AND bank_account_id IS NOT NULL AND offset_account_id IS NULL) OR "
            "(kind = 'adjustment' AND bank_account_id IS NULL AND offset_account_id IS NOT NULL) OR "
            "(kind = 'control_difference' AND amount < 0 AND bank_account_id IS NULL "
            "AND offset_account_id IS NOT NULL)",
            name=f"ck_{TABLE}_kind_shape",
        ),
        sa.CheckConstraint(
            "state <> 'approved' OR (approved_by_party_id IS NOT NULL AND approved_at IS NOT NULL)",
            name=f"ck_{TABLE}_approved",
        ),
        sa.CheckConstraint(
            "state <> 'recorded' OR (approved_by_party_id IS NOT NULL AND recorded_by_party_id IS NOT NULL "
            "AND recorded_at IS NOT NULL "
            "AND ((kind = 'opening' AND journal_id IS NULL) OR (kind <> 'opening' AND journal_id IS NOT NULL)) "
            "AND (kind <> 'advance' OR (bank_reference IS NOT NULL AND length(btrim(bank_reference)) > 0)))",
            name=f"ck_{TABLE}_recorded",
        ),
        sa.CheckConstraint(
            "NOT dual_control OR state = 'draft' OR approved_by_party_id <> prepared_by_party_id",
            name=f"ck_{TABLE}_approve_sod",
        ),
        sa.CheckConstraint(
            "NOT dual_control OR state <> 'recorded' OR (recorded_by_party_id <> prepared_by_party_id "
            "AND recorded_by_party_id <> approved_by_party_id)",
            name=f"ck_{TABLE}_record_sod",
        ),
        sa.CheckConstraint("bank_reference IS NULL OR kind = 'advance'", name=f"ck_{TABLE}_bank_reference_kind"),
    )
    op.create_index(
        f"uq_{TABLE}_bank_reference",
        TABLE,
        ["tenant_id", "bank_account_id", "bank_reference"],
        unique=True,
        postgresql_where=sa.text("bank_reference IS NOT NULL"),
    )
    op.create_index(
        f"uq_{TABLE}_journal",
        TABLE,
        ["tenant_id", "journal_id"],
        unique=True,
        postgresql_where=sa.text("journal_id IS NOT NULL"),
    )
    op.create_index(f"ix_{TABLE}_employee", TABLE, ["tenant_id", "employee_ref", "state"])
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON public.{TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    # Amounts, kind and membership never change; a recorded entry is final (correct it with an adjustment).
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _IMMUTABLE)
    op.execute(
        f"""
        CREATE FUNCTION public.{TABLE}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF {changed} THEN
                RAISE EXCEPTION 'staff loan entry amounts and membership are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.state = 'recorded' THEN
                RAISE EXCEPTION 'a recorded staff loan entry is final'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NOT ((OLD.state = 'draft' AND NEW.state IN ('draft', 'approved'))
                    OR (OLD.state = 'approved' AND NEW.state IN ('approved', 'recorded'))) THEN
                RAISE EXCEPTION 'invalid staff loan entry state transition'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_guard BEFORE UPDATE ON public.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{TABLE}_guard()"
    )
    op.execute(f"GRANT SELECT, INSERT ON public.{TABLE} TO app_user")
    op.execute(f"GRANT UPDATE ({', '.join(UPDATE_COLUMNS)}) ON public.{TABLE} TO app_user")


def downgrade() -> None:
    # The entries are the only subledger behind 1410: never drop them.
    exists = op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{TABLE})")).scalar()  # noqa: S608
    if exists:
        raise RuntimeError(f"refusing to drop {TABLE}: staff loan entries exist")
    op.drop_table(TABLE)
    op.execute(f"DROP FUNCTION IF EXISTS public.{TABLE}_guard()")
