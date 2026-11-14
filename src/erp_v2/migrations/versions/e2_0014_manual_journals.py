"""Manual-journal maker-checker state (B2). Additive; no cross-lineage dependency."""

# ruff: noqa: E501  (trigger SQL)

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0014_manual_journals"
down_revision = "e2_0013_banking_observations"
branch_labels = None
depends_on = None

_TABLE = "erp_manual_journals"

# Identity, preparer and every recorded actor/time are frozen once set; only the declared state
# transitions are allowed. The one exception is a held legacy journal re-requested after rejection.
_FUNCTION = """
CREATE FUNCTION public.erp_manual_journals_freeze() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    legacy_rerequest boolean;
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.journal_id IS DISTINCT FROM OLD.journal_id OR NEW.origin IS DISTINCT FROM OLD.origin
       OR NEW.legacy_source_key IS DISTINCT FROM OLD.legacy_source_key THEN
        RAISE EXCEPTION 'manual journal identity is immutable' USING ERRCODE = '23000';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
        (OLD.state = 'PREPARED' AND NEW.state = 'POST_REQUESTED')
        OR (OLD.state = 'POST_REQUESTED' AND NEW.state IN ('POSTED', 'REJECTED'))
        OR (OLD.state = 'POSTED' AND NEW.state = 'REVERSAL_REQUESTED')
        OR (OLD.state = 'REVERSAL_REQUESTED' AND NEW.state IN ('REVERSED', 'POSTED'))
        OR (OLD.state = 'REJECTED' AND NEW.state = 'POST_REQUESTED' AND OLD.origin = 'legacy_held')
    ) THEN
        RAISE EXCEPTION 'invalid manual journal state transition' USING ERRCODE = '23000';
    END IF;
    legacy_rerequest := OLD.state = 'REJECTED' AND NEW.state = 'POST_REQUESTED';
    IF NOT legacy_rerequest THEN
        IF NEW.evidence_ref IS DISTINCT FROM OLD.evidence_ref
           OR NEW.prepared_by_party_id IS DISTINCT FROM OLD.prepared_by_party_id
           OR NEW.prepared_at IS DISTINCT FROM OLD.prepared_at
           OR (OLD.requested_by_party_id IS NOT NULL AND NEW.requested_by_party_id IS DISTINCT FROM OLD.requested_by_party_id)
           OR (OLD.requested_at IS NOT NULL AND NEW.requested_at IS DISTINCT FROM OLD.requested_at)
           OR (OLD.approved_by_party_id IS NOT NULL AND NEW.approved_by_party_id IS DISTINCT FROM OLD.approved_by_party_id)
           OR (OLD.approved_at IS NOT NULL AND NEW.approved_at IS DISTINCT FROM OLD.approved_at)
           OR (OLD.rejected_by_party_id IS NOT NULL AND NEW.rejected_by_party_id IS DISTINCT FROM OLD.rejected_by_party_id)
           OR (OLD.rejected_at IS NOT NULL AND NEW.rejected_at IS DISTINCT FROM OLD.rejected_at)
           OR (OLD.rejection_reason IS NOT NULL AND NEW.rejection_reason IS DISTINCT FROM OLD.rejection_reason)
           OR (OLD.reversal_approved_by_party_id IS NOT NULL
               AND NEW.reversal_approved_by_party_id IS DISTINCT FROM OLD.reversal_approved_by_party_id)
           OR (OLD.reversal_approved_at IS NOT NULL AND NEW.reversal_approved_at IS DISTINCT FROM OLD.reversal_approved_at)
           OR (OLD.reversal_journal_id IS NOT NULL AND NEW.reversal_journal_id IS DISTINCT FROM OLD.reversal_journal_id)
        THEN
            RAISE EXCEPTION 'recorded maker-checker evidence is immutable' USING ERRCODE = '23000';
        END IF;
        -- A reversal request may only change while it is pending, or be cleared when withdrawn.
        IF OLD.state IN ('REVERSAL_REQUESTED', 'REVERSED') AND NOT (OLD.state = 'REVERSAL_REQUESTED' AND NEW.state = 'POSTED')
           AND (NEW.reversal_requested_by_party_id IS DISTINCT FROM OLD.reversal_requested_by_party_id
                OR NEW.reversal_requested_at IS DISTINCT FROM OLD.reversal_requested_at
                OR NEW.reversal_reason IS DISTINCT FROM OLD.reversal_reason
                OR NEW.reversal_posting_date IS DISTINCT FROM OLD.reversal_posting_date) THEN
            RAISE EXCEPTION 'recorded maker-checker evidence is immutable' USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$
"""


def _party(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), sa.ForeignKey("parties.id"), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("evidence_ref", sa.String(255), nullable=False),
        sa.Column("legacy_source_key", sa.String(480)),
        _party("prepared_by_party_id", nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        _party("requested_by_party_id"),
        sa.Column("requested_at", sa.DateTime(timezone=True)),
        _party("approved_by_party_id"),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        _party("rejected_by_party_id"),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column("rejection_reason", sa.Text()),
        _party("reversal_requested_by_party_id"),
        sa.Column("reversal_requested_at", sa.DateTime(timezone=True)),
        sa.Column("reversal_reason", sa.Text()),
        sa.Column("reversal_posting_date", sa.Date()),
        _party("reversal_approved_by_party_id"),
        sa.Column("reversal_approved_at", sa.DateTime(timezone=True)),
        sa.Column("reversal_journal_id", sa.Uuid()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_erp_manual_journals_tenant_id"),
        sa.UniqueConstraint("tenant_id", "journal_id", name="uq_erp_manual_journals_journal"),
        sa.CheckConstraint("origin IN ('manual', 'legacy_held')", name="ck_erp_manual_journals_origin"),
        sa.CheckConstraint(
            "state IN ('PREPARED', 'POST_REQUESTED', 'POSTED', 'REJECTED', 'REVERSAL_REQUESTED', 'REVERSED')",
            name="ck_erp_manual_journals_state",
        ),
        sa.CheckConstraint("length(btrim(evidence_ref)) > 0", name="ck_erp_manual_journals_evidence"),
        sa.CheckConstraint(
            "approved_by_party_id IS NULL OR (requested_by_party_id IS NOT NULL "
            "AND approved_by_party_id <> prepared_by_party_id "
            "AND approved_by_party_id <> requested_by_party_id)",
            name="ck_erp_manual_journals_post_sod",
        ),
        sa.CheckConstraint(
            "reversal_approved_by_party_id IS NULL OR (reversal_requested_by_party_id IS NOT NULL "
            "AND reversal_approved_by_party_id <> reversal_requested_by_party_id "
            "AND reversal_approved_by_party_id <> prepared_by_party_id)",
            name="ck_erp_manual_journals_reversal_sod",
        ),
        sa.CheckConstraint(
            "state NOT IN ('POST_REQUESTED', 'POSTED', 'REVERSAL_REQUESTED', 'REVERSED') "
            "OR (requested_by_party_id IS NOT NULL AND requested_at IS NOT NULL)",
            name="ck_erp_manual_journals_requested",
        ),
        sa.CheckConstraint(
            "state NOT IN ('POSTED', 'REVERSAL_REQUESTED', 'REVERSED') "
            "OR (approved_by_party_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_erp_manual_journals_posted",
        ),
        sa.CheckConstraint(
            "state NOT IN ('REVERSAL_REQUESTED', 'REVERSED') "
            "OR (reversal_requested_by_party_id IS NOT NULL AND reversal_reason IS NOT NULL "
            "AND reversal_posting_date IS NOT NULL)",
            name="ck_erp_manual_journals_reversal_requested",
        ),
        sa.CheckConstraint(
            "state <> 'REVERSED' OR (reversal_approved_by_party_id IS NOT NULL AND reversal_journal_id IS NOT NULL)",
            name="ck_erp_manual_journals_reversed",
        ),
        sa.CheckConstraint(
            "state <> 'REJECTED' OR (rejected_by_party_id IS NOT NULL AND rejection_reason IS NOT NULL)",
            name="ck_erp_manual_journals_rejected",
        ),
    )
    op.execute(f"ALTER TABLE public.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_TABLE}_tenant_isolation ON public.{_TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    # Never deleted: the maker-checker trail is append/transition only.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON public.{_TABLE} TO app_user")
    op.execute(_FUNCTION)
    op.execute(
        f"CREATE TRIGGER {_TABLE}_freeze BEFORE UPDATE ON public.{_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{_TABLE}_freeze()"
    )


def downgrade() -> None:
    # The maker-checker trail is evidence for posted journals: never discard it.
    op.execute(f"LOCK TABLE public.{_TABLE} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{_TABLE})")).scalar():  # noqa: S608
        raise RuntimeError("refusing to drop erp_manual_journals: it holds maker-checker evidence")
    op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_freeze ON public.{_TABLE}")
    op.drop_table(_TABLE)
    op.execute(f"DROP FUNCTION IF EXISTS public.{_TABLE}_freeze()")
