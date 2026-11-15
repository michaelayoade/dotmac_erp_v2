"""Bank reconciliation (B7): reconciliations, GL/statement matches, typed reconciling items.

Additive: four new tables and their guard functions; no change to any existing object and no
foreign key into accounting (ledger line ids are plain columns resolved inside the tenant by the
service; accounting a1 owns the ledger and B7 never writes it).

Security posture:
- RLS forced with a tenant isolation policy on every table, like every other e2 table.
- The runtime role has no DELETE on any of the four tables;
  items are insert-only (withdrawal columns only, while OPEN). UPDATE is column-limited to lifecycle columns.
- An APPROVED (or CANCELLED) reconciliation is immutable: any UPDATE is refused by trigger, its
  items cannot change, and the matches it covers are locked (cannot be unmatched).
- CHECK constraints enforce the two-person rule (approver differs from preparer) and the zero
  difference gate: approved => difference = 0 and statement closing = GL balance + items.
"""

# ruff: noqa: E501  (trigger SQL)

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0017_bank_reconciliation"
down_revision = "e2_0016_legacy_invoice_refs"
branch_labels = None
depends_on = None

RECON = "erp_bank_reconciliations"
MATCH = "erp_bank_recon_matches"
MATCH_LINE = "erp_bank_recon_match_lines"
ITEM = "erp_bank_recon_items"
_TABLES = (RECON, MATCH, MATCH_LINE, ITEM)

_RECON_FN = """
CREATE FUNCTION public.erp_bank_reconciliations_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank reconciliations are never deleted' USING ERRCODE = '23000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.account_id IS DISTINCT FROM OLD.account_id OR NEW.statement_id IS DISTINCT FROM OLD.statement_id
       OR NEW.gl_account_id IS DISTINCT FROM OLD.gl_account_id OR NEW.period_start IS DISTINCT FROM OLD.period_start
       OR NEW.period_end IS DISTINCT FROM OLD.period_end OR NEW.currency_code IS DISTINCT FROM OLD.currency_code
       OR NEW.opened_by_party_id IS DISTINCT FROM OLD.opened_by_party_id OR NEW.opened_at IS DISTINCT FROM OLD.opened_at THEN
        RAISE EXCEPTION 'reconciliation identity is immutable' USING ERRCODE = '23000';
    END IF;
    IF OLD.status IN ('approved', 'cancelled') THEN
        RAISE EXCEPTION 'an approved or cancelled reconciliation is immutable' USING ERRCODE = '23000';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'open' AND NEW.status IN ('prepared', 'cancelled'))
        OR (OLD.status = 'prepared' AND NEW.status IN ('approved', 'open', 'cancelled'))
    ) THEN
        RAISE EXCEPTION 'invalid reconciliation state transition' USING ERRCODE = '23000';
    END IF;
    IF OLD.status = 'prepared' AND NEW.status = 'approved' THEN
        IF NEW.statement_closing_balance IS DISTINCT FROM OLD.statement_closing_balance
           OR NEW.gl_balance IS DISTINCT FROM OLD.gl_balance OR NEW.items_total IS DISTINCT FROM OLD.items_total
           OR NEW.difference IS DISTINCT FROM OLD.difference OR NEW.snapshot_sha256 IS DISTINCT FROM OLD.snapshot_sha256
           OR NEW.prepared_by_party_id IS DISTINCT FROM OLD.prepared_by_party_id
           OR NEW.prepared_at IS DISTINCT FROM OLD.prepared_at THEN
            RAISE EXCEPTION 'the prepared snapshot is frozen at approval' USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$
"""

_MATCH_FN = """
CREATE FUNCTION public.erp_bank_recon_matches_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank reconciliation matches are never deleted' USING ERRCODE = '23000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.account_id IS DISTINCT FROM OLD.account_id OR NEW.member_sha256 IS DISTINCT FROM OLD.member_sha256
       OR NEW.net_amount IS DISTINCT FROM OLD.net_amount OR NEW.latest_date IS DISTINCT FROM OLD.latest_date
       OR NEW.matched_by_party_id IS DISTINCT FROM OLD.matched_by_party_id OR NEW.matched_at IS DISTINCT FROM OLD.matched_at THEN
        RAISE EXCEPTION 'match identity is immutable' USING ERRCODE = '23000';
    END IF;
    IF OLD.locked_by_reconciliation_id IS NOT NULL THEN
        RAISE EXCEPTION 'a match covered by an approved reconciliation is immutable' USING ERRCODE = '23000';
    END IF;
    IF OLD.status = 'unmatched' THEN
        RAISE EXCEPTION 'an unmatched match is final' USING ERRCODE = '23000';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NEW.status <> 'unmatched' THEN
        RAISE EXCEPTION 'invalid match state transition' USING ERRCODE = '23000';
    END IF;
    IF NEW.locked_by_reconciliation_id IS NOT NULL AND NEW.status <> 'active' THEN
        RAISE EXCEPTION 'only an active match can be locked' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END
$fn$
"""

_MATCH_LINE_FN = """
CREATE FUNCTION public.erp_bank_recon_match_lines_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    locked uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank reconciliation match lines are never deleted' USING ERRCODE = '23000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.match_id IS DISTINCT FROM OLD.match_id OR NEW.side IS DISTINCT FROM OLD.side
       OR NEW.ref_id IS DISTINCT FROM OLD.ref_id OR NEW.amount IS DISTINCT FROM OLD.amount THEN
        RAISE EXCEPTION 'match line identity is immutable' USING ERRCODE = '23000';
    END IF;
    IF OLD.active = false AND NEW.active = true THEN
        RAISE EXCEPTION 'a released match line cannot be re-activated' USING ERRCODE = '23000';
    END IF;
    SELECT locked_by_reconciliation_id INTO locked FROM public.erp_bank_recon_matches
        WHERE id = OLD.match_id AND tenant_id = OLD.tenant_id;
    IF locked IS NOT NULL THEN
        RAISE EXCEPTION 'a match covered by an approved reconciliation is immutable' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END
$fn$
"""

_ITEM_FN = """
CREATE FUNCTION public.erp_bank_recon_items_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    parent_status text;
    parent_id uuid;
    parent_tenant uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'reconciling items are never deleted; withdraw them' USING ERRCODE = '23000';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- The one allowed change: a governed withdrawal of a still-active item.
        IF OLD.withdrawn_at IS NOT NULL OR NEW.withdrawn_at IS NULL
           OR NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.reconciliation_id IS DISTINCT FROM OLD.reconciliation_id OR NEW.side IS DISTINCT FROM OLD.side
           OR NEW.ref_id IS DISTINCT FROM OLD.ref_id OR NEW.item_type IS DISTINCT FROM OLD.item_type
           OR NEW.signed_amount IS DISTINCT FROM OLD.signed_amount OR NEW.note IS DISTINCT FROM OLD.note
           OR NEW.created_by_party_id IS DISTINCT FROM OLD.created_by_party_id THEN
            RAISE EXCEPTION 'reconciling items are insert-only; only a withdrawal is allowed' USING ERRCODE = '23000';
        END IF;
    END IF;
    parent_id := NEW.reconciliation_id;
    parent_tenant := NEW.tenant_id;
    SELECT status INTO parent_status FROM public.erp_bank_reconciliations
        WHERE id = parent_id AND tenant_id = parent_tenant;
    IF parent_status IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION 'reconciling items can change only while the reconciliation is open' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END
$fn$
"""


def _common():
    return (
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )


def _stamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def _party(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), sa.ForeignKey("parties.id"), nullable=nullable)


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )


def upgrade() -> None:
    money = sa.Numeric(20, 6)
    op.create_table(
        RECON,
        *_common(),
        *_stamps(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("statement_id", sa.Uuid(), nullable=False),
        sa.Column("gl_account_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("statement_closing_balance", money),
        sa.Column("gl_balance", money),
        sa.Column("items_total", money),
        sa.Column("difference", money),
        sa.Column("snapshot_sha256", sa.String(64)),
        _party("opened_by_party_id", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        _party("prepared_by_party_id"),
        sa.Column("prepared_at", sa.DateTime(timezone=True)),
        _party("approved_by_party_id"),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        _party("returned_by_party_id"),
        sa.Column("returned_at", sa.DateTime(timezone=True)),
        sa.Column("return_reason", sa.String(500)),
        _party("cancelled_by_party_id"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_reason", sa.String(500)),
        sa.UniqueConstraint("tenant_id", "id", name="uq_erp_bank_reconciliations_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"], ["mod_banking.bank_accounts.tenant_id", "mod_banking.bank_accounts.id"]
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "statement_id"],
            ["mod_banking.bank_statements.tenant_id", "mod_banking.bank_statements.id"],
        ),
        sa.CheckConstraint(
            "status IN ('open', 'prepared', 'approved', 'cancelled')", name="ck_erp_bank_reconciliations_status"
        ),
        sa.CheckConstraint("period_end >= period_start", name="ck_erp_bank_reconciliations_period"),
        sa.CheckConstraint(
            "status NOT IN ('prepared', 'approved') OR (prepared_by_party_id IS NOT NULL AND prepared_at IS NOT NULL "
            "AND statement_closing_balance IS NOT NULL AND gl_balance IS NOT NULL AND items_total IS NOT NULL "
            "AND difference IS NOT NULL AND snapshot_sha256 IS NOT NULL)",
            name="ck_erp_bank_reconciliations_prepared",
        ),
        # The arithmetic is stored, never trusted: difference is exactly closing - (GL + items).
        sa.CheckConstraint(
            "difference IS NULL OR difference = statement_closing_balance - (gl_balance + items_total)",
            name="ck_erp_bank_reconciliations_arithmetic",
        ),
        # Zero-difference gate and the two-person rule, enforced by the database as well.
        sa.CheckConstraint(
            "status <> 'approved' OR (approved_by_party_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND difference = 0 AND approved_by_party_id <> prepared_by_party_id "
            "AND approved_by_party_id <> opened_by_party_id)",
            name="ck_erp_bank_reconciliations_approved",
        ),
        sa.CheckConstraint(
            "approved_by_party_id IS NULL OR status = 'approved'", name="ck_erp_bank_reconciliations_approver_status"
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR (cancelled_by_party_id IS NOT NULL AND cancelled_at IS NOT NULL "
            "AND cancel_reason IS NOT NULL AND length(btrim(cancel_reason)) > 0)",
            name="ck_erp_bank_reconciliations_cancelled",
        ),
    )
    live = sa.text("status IN ('open', 'prepared', 'approved')")
    op.create_index(
        "uq_erp_bank_reconciliations_period",
        RECON,
        ["tenant_id", "account_id", "period_end"],
        unique=True,
        postgresql_where=live,
    )
    op.create_index(
        "uq_erp_bank_reconciliations_statement",
        RECON,
        ["tenant_id", "statement_id"],
        unique=True,
        postgresql_where=live,
    )
    op.create_table(
        MATCH,
        *_common(),
        *_stamps(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.Column("net_amount", money, nullable=False),
        sa.Column("latest_date", sa.Date(), nullable=False),
        _party("matched_by_party_id", nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False),
        _party("unmatched_by_party_id"),
        sa.Column("unmatched_at", sa.DateTime(timezone=True)),
        sa.Column("unmatch_reason", sa.String(500)),
        sa.Column("locked_by_reconciliation_id", sa.Uuid()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_erp_bank_recon_matches_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"], ["mod_banking.bank_accounts.tenant_id", "mod_banking.bank_accounts.id"]
        ),
        sa.ForeignKeyConstraint(["tenant_id", "locked_by_reconciliation_id"], [f"{RECON}.tenant_id", f"{RECON}.id"]),
        sa.CheckConstraint("status IN ('active', 'unmatched')", name="ck_erp_bank_recon_matches_status"),
        sa.CheckConstraint(
            "status <> 'unmatched' OR (unmatched_by_party_id IS NOT NULL AND unmatched_at IS NOT NULL "
            "AND unmatch_reason IS NOT NULL AND length(btrim(unmatch_reason)) > 0)",
            name="ck_erp_bank_recon_matches_unmatched",
        ),
        sa.CheckConstraint(
            "locked_by_reconciliation_id IS NULL OR status = 'active'", name="ck_erp_bank_recon_matches_locked"
        ),
    )
    op.create_index(
        "uq_erp_bank_recon_matches_members",
        MATCH,
        ["tenant_id", "account_id", "member_sha256"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_erp_bank_recon_matches_account", MATCH, ["tenant_id", "account_id", "status", "latest_date"])
    op.create_table(
        MATCH_LINE,
        *_common(),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=False),
        sa.Column("amount", money, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["tenant_id", "match_id"], [f"{MATCH}.tenant_id", f"{MATCH}.id"]),
        sa.CheckConstraint("side IN ('bank', 'gl')", name="ck_erp_bank_recon_match_lines_side"),
        sa.CheckConstraint("amount <> 0", name="ck_erp_bank_recon_match_lines_amount"),
    )
    # One active match per statement line and per ledger line: the database refuses a double match.
    op.create_index(
        "uq_erp_bank_recon_match_lines_active",
        MATCH_LINE,
        ["tenant_id", "side", "ref_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_index("ix_erp_bank_recon_match_lines_match", MATCH_LINE, ["tenant_id", "match_id"])
    op.create_table(
        ITEM,
        *_common(),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reconciliation_id", sa.Uuid(), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.String(32), nullable=False),
        sa.Column("signed_amount", money, nullable=False),
        sa.Column("note", sa.String(500), nullable=False),
        _party("created_by_party_id", nullable=False),
        _party("withdrawn_by_party_id"),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.Column("withdraw_reason", sa.String(500)),
        sa.ForeignKeyConstraint(["tenant_id", "reconciliation_id"], [f"{RECON}.tenant_id", f"{RECON}.id"]),
        sa.CheckConstraint(
            "(side = 'gl' AND item_type IN ('deposit_in_transit', 'outstanding_payment')) "
            "OR (side = 'bank' AND item_type IN ('unrecorded_bank_charge', 'unrecorded_bank_receipt'))",
            name="ck_erp_bank_recon_items_type",
        ),
        # signed_amount is the item's contribution to GL balance + items = statement closing.
        sa.CheckConstraint(
            "(item_type IN ('deposit_in_transit', 'unrecorded_bank_charge') AND signed_amount < 0) "
            "OR (item_type IN ('outstanding_payment', 'unrecorded_bank_receipt') AND signed_amount > 0)",
            name="ck_erp_bank_recon_items_sign",
        ),
        sa.CheckConstraint("length(btrim(note)) > 0", name="ck_erp_bank_recon_items_note"),
        sa.CheckConstraint(
            "(withdrawn_at IS NULL AND withdrawn_by_party_id IS NULL AND withdraw_reason IS NULL) "
            "OR (withdrawn_at IS NOT NULL AND withdrawn_by_party_id IS NOT NULL "
            "AND withdraw_reason IS NOT NULL AND length(btrim(withdraw_reason)) > 0)",
            name="ck_erp_bank_recon_items_withdrawn",
        ),
    )
    # One ACTIVE item per line and reconciliation; withdrawn rows stay as the audit trail.
    op.create_index(
        "uq_erp_bank_recon_items_line",
        ITEM,
        ["tenant_id", "reconciliation_id", "side", "ref_id"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL"),
    )
    for table in _TABLES:
        _rls(table)
    op.execute(f"GRANT SELECT, INSERT ON public.{RECON} TO app_user")
    op.execute(
        f"GRANT UPDATE (status, statement_closing_balance, gl_balance, items_total, difference, snapshot_sha256, "
        f"prepared_by_party_id, prepared_at, approved_by_party_id, approved_at, returned_by_party_id, returned_at, "
        f"return_reason, cancelled_by_party_id, cancelled_at, cancel_reason, updated_at) ON public.{RECON} TO app_user"
    )
    op.execute(f"GRANT SELECT, INSERT ON public.{MATCH} TO app_user")
    op.execute(
        f"GRANT UPDATE (status, unmatched_by_party_id, unmatched_at, unmatch_reason, locked_by_reconciliation_id, "
        f"updated_at) ON public.{MATCH} TO app_user"
    )
    op.execute(f"GRANT SELECT, INSERT ON public.{MATCH_LINE} TO app_user")
    op.execute(f"GRANT UPDATE (active) ON public.{MATCH_LINE} TO app_user")
    op.execute(f"GRANT SELECT, INSERT ON public.{ITEM} TO app_user")
    op.execute(f"GRANT UPDATE (withdrawn_by_party_id, withdrawn_at, withdraw_reason) ON public.{ITEM} TO app_user")
    for function, table, events in (
        (_RECON_FN, RECON, "UPDATE OR DELETE"),
        (_MATCH_FN, MATCH, "UPDATE OR DELETE"),
        (_MATCH_LINE_FN, MATCH_LINE, "UPDATE OR DELETE"),
        (_ITEM_FN, ITEM, "INSERT OR UPDATE OR DELETE"),
    ):
        op.execute(function)
        op.execute(
            f"CREATE TRIGGER {table}_guard BEFORE {events} ON public.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION public.{table}_guard()"
        )


def downgrade() -> None:
    # Reconciliations are evidence for closed periods: never discard them.
    bind = op.get_bind()
    for table in _TABLES:
        op.execute(f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE")
    for table in _TABLES:
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{table})")).scalar():  # noqa: S608
            raise RuntimeError(f"refusing to drop {table}: bank reconciliation evidence exists")
    for table in (ITEM, MATCH_LINE, MATCH, RECON):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_guard ON public.{table}")
        op.drop_table(table)
        op.execute(f"DROP FUNCTION IF EXISTS public.{table}_guard()")
