"""B-16 AR producer: four ERP-owned workflow tables for receipts, invoices and credit notes.

Additive: four new tables, no change to any existing object. FORCE RLS with the tenant policy on all four.

W5 (explicit widening, approved by Michael 2026-11-19): the producer runs as the runtime role, so ``app_user`` gets

* ``erp_ar_drafts``: SELECT, INSERT and a column-limited UPDATE (editable fields, status, issuer, billing ids);
  no DELETE (a draft is ``deleted`` by status, never removed);
* ``erp_ar_draft_lines``: SELECT, INSERT, UPDATE, DELETE, bounded by a trigger to drafts whose header is
  still ``draft``;
* ``erp_ar_line_accounts`` and ``erp_ar_receipts``: SELECT and INSERT only (append-only triggers, which also bind
  the owner role).

Triggers hold the invariants even against the owner role: a draft moves only draft -> issued | deleted and both are
terminal; its identity and D-9 stamp never change; billing ids are set once, with ``issued``.

``revenue_account_id`` columns are plain ids, not foreign keys into ``mod_accounting`` (the manual-journal
precedent: this lineage takes no cross-lineage migration dependency); the service resolves each account in the
tenant before use.

The downgrade refuses while any row exists.

Revision chain: e2_0031_billing_fact_postings (B-15) -> this.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.ar.models import DRAFT_LINES_TABLE, DRAFTS_TABLE, LINE_ACCOUNTS_TABLE, RECEIPTS_TABLE

revision = "e2_0032_ar_producer"
down_revision = "e2_0031_billing_fact_postings"
branch_labels = None
depends_on = None

_MONEY = sa.Numeric(20, 6)


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )


def _trigger(table: str, name: str, events: str, body: str) -> None:
    op.execute(f"CREATE FUNCTION public.{table}_{name}() RETURNS trigger LANGUAGE plpgsql AS $$ {body} $$")
    op.execute(
        f"CREATE TRIGGER {table}_{name} BEFORE {events} ON public.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{table}_{name}()"
    )


def upgrade() -> None:
    op.create_table(
        DRAFTS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("customer_account_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("original_document_id", sa.Uuid(), nullable=True),
        sa.Column("bank_account_id", sa.Uuid(), nullable=True),
        sa.Column("bank_reference", sa.String(255), nullable=True),
        sa.Column("amount", _MONEY, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payment_terms_code", sa.String(80), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("prepared_by", sa.Uuid(), nullable=False),
        sa.Column("issued_by", sa.Uuid(), nullable=True),
        sa.Column("dual_control", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("billing_document_id", sa.Uuid(), nullable=True),
        sa.Column("billing_settlement_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{DRAFTS_TABLE}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "request_id", name=f"uq_{DRAFTS_TABLE}_request"),
        sa.CheckConstraint("kind IN ('invoice', 'credit_note', 'receipt')", name=f"ck_{DRAFTS_TABLE}_kind"),
        sa.CheckConstraint("status IN ('draft', 'issued', 'deleted')", name=f"ck_{DRAFTS_TABLE}_status"),
        sa.CheckConstraint(
            "NOT dual_control OR issued_by IS NULL OR prepared_by <> issued_by", name=f"ck_{DRAFTS_TABLE}_dual"
        ),
        sa.CheckConstraint("due_date IS NULL OR due_date >= document_date", name=f"ck_{DRAFTS_TABLE}_due_date"),
        sa.CheckConstraint("(status = 'issued') = (issued_by IS NOT NULL)", name=f"ck_{DRAFTS_TABLE}_issued_by"),
        sa.CheckConstraint(
            "(billing_document_id IS NULL OR billing_settlement_id IS NULL) "
            "AND ((billing_document_id IS NULL AND billing_settlement_id IS NULL) OR status = 'issued') "
            "AND (kind = 'receipt' OR billing_settlement_id IS NULL) "
            "AND (kind <> 'receipt' OR billing_document_id IS NULL)",
            name=f"ck_{DRAFTS_TABLE}_billing_ids",
        ),
    )
    op.create_index(f"ix_{DRAFTS_TABLE}_status", DRAFTS_TABLE, ["tenant_id", "kind", "status"])
    _rls(DRAFTS_TABLE)
    _trigger(
        DRAFTS_TABLE,
        "guard",
        "UPDATE OR DELETE",
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'AR drafts are never deleted; mark them deleted' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.kind <> OLD.kind "
        "OR NEW.request_id <> OLD.request_id OR NEW.prepared_by <> OLD.prepared_by "
        "OR NEW.dual_control <> OLD.dual_control OR NEW.created_at <> OLD.created_at THEN "
        "RAISE EXCEPTION 'AR draft identity and dual-control stamp are immutable' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF OLD.status <> 'draft' THEN RAISE EXCEPTION 'an issued or deleted AR draft is terminal' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.status = 'deleted' AND (NEW.billing_document_id IS NOT NULL "
        "OR NEW.billing_settlement_id IS NOT NULL) THEN "
        "RAISE EXCEPTION 'a deleted AR draft carries no billing id' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "RETURN NEW; END",
    )
    op.execute(f"GRANT SELECT, INSERT ON public.{DRAFTS_TABLE} TO app_user")
    op.execute(
        f"GRANT UPDATE (customer_account_id, currency, document_date, original_document_id, bank_account_id, "
        f"bank_reference, amount, reason, payment_terms_code, due_date, status, issued_by, billing_document_id, "
        f"billing_settlement_id, updated_at) ON public.{DRAFTS_TABLE} TO app_user"
    )

    op.create_table(
        DRAFT_LINES_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("quantity", _MONEY, nullable=True),
        sa.Column("unit_amount", _MONEY, nullable=True),
        sa.Column("revenue_account_id", sa.Uuid(), nullable=True),
        sa.Column("tax_code_id", sa.Uuid(), nullable=True),
        sa.Column("target_document_id", sa.Uuid(), nullable=True),
        sa.Column("amount", _MONEY, nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            [f"{DRAFTS_TABLE}.tenant_id", f"{DRAFTS_TABLE}.id"],
            ondelete="CASCADE",
            name=f"fk_{DRAFT_LINES_TABLE}_draft",
        ),
        sa.UniqueConstraint("tenant_id", "draft_id", "n", name=f"uq_{DRAFT_LINES_TABLE}_n"),
        sa.CheckConstraint("n >= 1", name=f"ck_{DRAFT_LINES_TABLE}_n"),
    )
    _rls(DRAFT_LINES_TABLE)
    _trigger(
        DRAFT_LINES_TABLE,
        "guard",
        "INSERT OR UPDATE OR DELETE",
        "DECLARE h text; d uuid; t uuid; BEGIN "
        "IF TG_OP = 'DELETE' THEN d := OLD.draft_id; t := OLD.tenant_id; ELSE d := NEW.draft_id; "
        "t := NEW.tenant_id; END IF; "
        "SELECT status INTO h FROM public.erp_ar_drafts WHERE tenant_id = t AND id = d; "
        "IF h IS DISTINCT FROM 'draft' THEN "
        "RAISE EXCEPTION 'AR draft lines change only while the draft is a draft' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF TG_OP = 'UPDATE' AND (NEW.draft_id <> OLD.draft_id OR NEW.tenant_id <> OLD.tenant_id) THEN "
        "RAISE EXCEPTION 'an AR draft line cannot move between drafts' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; END",
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{DRAFT_LINES_TABLE} TO app_user")

    op.create_table(
        LINE_ACCOUNTS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("billing_document_id", sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("obligation_id", sa.Uuid(), nullable=True),
        sa.Column("revenue_account_id", sa.Uuid(), nullable=False),
        sa.Column("pre_tax", _MONEY, nullable=False),
        sa.Column("tax", _MONEY, nullable=False),
        sa.Column("treatment_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "billing_document_id", "line_number", name=f"uq_{LINE_ACCOUNTS_TABLE}_line"),
        sa.CheckConstraint("line_number >= 1", name=f"ck_{LINE_ACCOUNTS_TABLE}_line"),
        sa.CheckConstraint("pre_tax >= 0 AND tax >= 0", name=f"ck_{LINE_ACCOUNTS_TABLE}_amounts"),
    )
    op.create_index(f"ix_{LINE_ACCOUNTS_TABLE}_document", LINE_ACCOUNTS_TABLE, ["tenant_id", "billing_document_id"])
    _rls(LINE_ACCOUNTS_TABLE)
    op.execute(f"GRANT SELECT, INSERT ON public.{LINE_ACCOUNTS_TABLE} TO app_user")

    op.create_table(
        RECEIPTS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("billing_settlement_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_id", sa.Uuid(), nullable=False),
        sa.Column("cash_account_id", sa.Uuid(), nullable=False),
        sa.Column("bank_reference", sa.String(255), nullable=False),
        sa.Column("receipt_number", sa.String(80), nullable=False),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("recorded_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "billing_settlement_id", name=f"uq_{RECEIPTS_TABLE}_settlement"),
        sa.UniqueConstraint("tenant_id", "draft_id", name=f"uq_{RECEIPTS_TABLE}_draft"),
        sa.UniqueConstraint("tenant_id", "bank_account_id", "bank_reference", name=f"uq_{RECEIPTS_TABLE}_bank_ref"),
        sa.UniqueConstraint("tenant_id", "receipt_number", name=f"uq_{RECEIPTS_TABLE}_number"),
    )
    _rls(RECEIPTS_TABLE)
    op.execute(f"GRANT SELECT, INSERT ON public.{RECEIPTS_TABLE} TO app_user")

    for table in (LINE_ACCOUNTS_TABLE, RECEIPTS_TABLE):
        _trigger(
            table,
            "append_only",
            "UPDATE OR DELETE",
            f"BEGIN RAISE EXCEPTION '{table} is append-only' USING ERRCODE = 'integrity_constraint_violation'; END",
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (RECEIPTS_TABLE, LINE_ACCOUNTS_TABLE, DRAFT_LINES_TABLE, DRAFTS_TABLE):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{table})")).scalar():  # noqa: S608
            raise RuntimeError(f"refusing to drop {table}: AR producer rows exist")
    for table in (RECEIPTS_TABLE, LINE_ACCOUNTS_TABLE, DRAFT_LINES_TABLE, DRAFTS_TABLE):
        op.drop_table(table)
    for table, names in (
        (RECEIPTS_TABLE, ("append_only",)),
        (LINE_ACCOUNTS_TABLE, ("append_only",)),
        (DRAFT_LINES_TABLE, ("guard",)),
        (DRAFTS_TABLE, ("guard",)),
    ):
        for name in names:
            op.execute(f"DROP FUNCTION IF EXISTS public.{table}_{name}()")
