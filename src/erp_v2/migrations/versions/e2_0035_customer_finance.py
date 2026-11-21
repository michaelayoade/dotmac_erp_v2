"""Customer screen (F-F): the ERP-owned finance row of a customer account.

Additive: one new table, ``erp_customer_finance`` (trading name, tax id, payment terms code, primary contact,
billing address), keyed by the plain customer account id (UNIQUE per tenant; no foreign key into ``mod_customers``,
the manual-journal precedent; the service resolves the account in the tenant). No JSON, no free metadata, never
financial identity. FORCE RLS with the tenant policy.

A trigger holds the invariants even against the owner role: id, tenant_id, customer_account_id and created_at are
immutable, and DELETE is refused (a customer is closed by status in dotmac-customers, never removed).

W10 (explicit widening, approved by Michael 2026-11-20): the screen runs as the runtime role, so ``app_user`` gets
SELECT, INSERT and a column-limited UPDATE (the editable columns, updated_at, updated_by). No DELETE. The wave-4
importer writes as the migration role.

The downgrade refuses while any row exists.

Revision chain: e2_0033_billing_gl_vat -> this. e2_0034 stays reserved for the WHT follow-up; when created it chains
after the then head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.customers_web import models as m

revision = "e2_0035_customer_finance"
down_revision = "e2_0033_billing_gl_vat"
branch_labels = None
depends_on = None

_T = m.TABLE


def upgrade() -> None:
    op.create_table(
        _T,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("customer_account_id", sa.Uuid(), nullable=False),
        sa.Column("trading_name", sa.String(m.MAX_TRADING_NAME), nullable=True),
        sa.Column("tax_identification_number", sa.String(m.MAX_TIN), nullable=True),
        sa.Column("payment_terms_code", sa.String(m.MAX_TERMS_CODE), nullable=True),
        sa.Column("contact_name", sa.String(m.MAX_CONTACT_NAME), nullable=True),
        sa.Column("contact_email", sa.String(m.MAX_EMAIL), nullable=True),
        sa.Column("contact_phone", sa.String(m.MAX_PHONE), nullable=True),
        sa.Column("billing_address", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "customer_account_id", name=f"uq_{_T}_customer"),
        sa.CheckConstraint(f"char_length(trading_name) <= {m.MAX_TRADING_NAME}", name=f"ck_{_T}_trading_name"),
        sa.CheckConstraint(f"char_length(tax_identification_number) <= {m.MAX_TIN}", name=f"ck_{_T}_tin"),
        sa.CheckConstraint(f"char_length(payment_terms_code) <= {m.MAX_TERMS_CODE}", name=f"ck_{_T}_terms"),
        sa.CheckConstraint(f"char_length(contact_name) <= {m.MAX_CONTACT_NAME}", name=f"ck_{_T}_contact_name"),
        sa.CheckConstraint(f"char_length(contact_email) <= {m.MAX_EMAIL}", name=f"ck_{_T}_contact_email"),
        sa.CheckConstraint(f"char_length(contact_phone) <= {m.MAX_PHONE}", name=f"ck_{_T}_contact_phone"),
        sa.CheckConstraint(f"char_length(billing_address) <= {m.MAX_ADDRESS}", name=f"ck_{_T}_address"),
    )
    op.execute(f"ALTER TABLE public.{_T} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{_T} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_T}_tenant_isolation ON public.{_T} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute(
        f"CREATE FUNCTION public.{_T}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'customer finance rows are never deleted' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.customer_account_id <> OLD.customer_account_id "
        "OR NEW.created_at <> OLD.created_at THEN "
        "RAISE EXCEPTION 'customer finance identity is immutable' "
        "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
        "RETURN NEW; END $$"
    )
    op.execute(
        f"CREATE TRIGGER {_T}_guard BEFORE UPDATE OR DELETE ON public.{_T} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{_T}_guard()"
    )
    op.execute(f"GRANT SELECT, INSERT ON public.{_T} TO app_user")
    op.execute(f"GRANT UPDATE ({', '.join(m.EDITABLE_COLUMNS)}) ON public.{_T} TO app_user")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{_T})")).scalar():  # noqa: S608
        raise RuntimeError(f"refusing to drop {_T}: customer finance rows exist")
    op.drop_table(_T)
    op.execute(f"DROP FUNCTION IF EXISTS public.{_T}_guard()")
