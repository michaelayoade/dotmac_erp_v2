"""Per-tenant dual-control policy (D-9; Michael 2026-11-17/18: no compulsory two-person, default OFF).

One policy row per tenant (``erp_governance_policies``; FORCE RLS; the runtime role may SELECT only, the
operator CLI writes it). The six tables that carry a database two-person/separation CHECK each gain
``dual_control boolean NOT NULL DEFAULT true``, so every row that exists today stays strict, and each CHECK
is rewritten as ``NOT dual_control OR (<existing predicate>)``. New rows take the tenant policy from the
service (a missing policy row reads as OFF). Where an existing CHECK mixed a separation term with a plain
integrity term (an approved row must carry its approver and time, a reconciliation must be clean, a re-point
must record its applied reference), the integrity terms are kept unconditional in a companion
``*_integrity`` CHECK so switching the policy off relaxes the separation only.

The B5 returner guard trigger is replaced with the same body gated on the line's ``dual_control``. The
runtime role cannot UPDATE ``dual_control`` on any of the five tables with column-level UPDATE grants; the
manual-journal table has a table-level UPDATE grant, so a small trigger freezes the column there.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0022_dual_control_policy"
down_revision = "e2_0021_payroll_payout_returns"
branch_labels = None
depends_on = None

_POLICY = "erp_governance_policies"
_MJ = "erp_manual_journals"
_PAYEE = "payee_bank_instructions"
_RECON = "erp_bank_reconciliations"
_B = "erp_payroll_payout_batches"
_L = "erp_payroll_payout_lines"
_CASH = "erp_cash_account_repoints"

_TABLES = (_MJ, _PAYEE, _RECON, _B, _L, _CASH)

# (table, constraint, original predicate, integrity companion predicate or None)
_RULES: tuple[tuple[str, str, str, str | None], ...] = (
    (
        _MJ,
        "ck_erp_manual_journals_post_sod",
        "approved_by_party_id IS NULL OR (requested_by_party_id IS NOT NULL "
        "AND approved_by_party_id <> prepared_by_party_id "
        "AND approved_by_party_id <> requested_by_party_id)",
        None,
    ),
    (
        _MJ,
        "ck_erp_manual_journals_reversal_sod",
        "reversal_approved_by_party_id IS NULL OR (reversal_requested_by_party_id IS NOT NULL "
        "AND reversal_approved_by_party_id <> reversal_requested_by_party_id "
        "AND reversal_approved_by_party_id <> prepared_by_party_id)",
        None,
    ),
    (
        _PAYEE,
        "ck_payee_bank_instructions_two_person",
        "status IN ('proposed', 'rejected') OR (approved_at IS NOT NULL "
        "AND decided_by_party_id IS NOT NULL "
        "AND (proposed_by_party_id IS NULL OR decided_by_party_id <> proposed_by_party_id))",
        "status IN ('proposed', 'rejected') OR (approved_at IS NOT NULL AND decided_by_party_id IS NOT NULL)",
    ),
    (
        _RECON,
        "ck_erp_bank_reconciliations_approved",
        "status <> 'approved' OR (approved_by_party_id IS NOT NULL AND approved_at IS NOT NULL "
        "AND difference = 0 AND approved_by_party_id <> prepared_by_party_id "
        "AND approved_by_party_id <> opened_by_party_id)",
        "status <> 'approved' OR (approved_by_party_id IS NOT NULL AND approved_at IS NOT NULL AND difference = 0)",
    ),
    (
        _B,
        f"ck_{_B}_distinct_people",
        "(approved_by_party_id IS NULL OR approved_by_party_id <> prepared_by_party_id) "
        "AND (file_issued_by_party_id IS NULL OR (file_issued_by_party_id <> prepared_by_party_id "
        "AND file_issued_by_party_id <> approved_by_party_id)) "
        "AND (recorded_by_party_id IS NULL OR (recorded_by_party_id <> prepared_by_party_id "
        "AND recorded_by_party_id <> approved_by_party_id "
        "AND recorded_by_party_id <> file_issued_by_party_id))",
        None,
    ),
    (
        _CASH,
        f"ck_{_CASH}_two_person",
        "status <> 'approved' OR (decided_by_party_id <> proposed_by_party_id AND applied_ref IS NOT NULL)",
        "status <> 'approved' OR applied_ref IS NOT NULL",
    ),
)

_GUARD_FN = f"public.{_L}_return_guard"
_MJ_FREEZE_FN = f"public.{_MJ}_dual_control_freeze"


def _return_guard(returner_condition: str) -> str:
    """The e2_0021 returner guard, with the returner test gated by ``returner_condition`` (``""`` = original)."""
    return f"""
        CREATE OR REPLACE FUNCTION {_GUARD_FN}() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE b public.{_B}%ROWTYPE;
        BEGIN
            IF OLD.returned_at IS NOT NULL THEN
                RAISE EXCEPTION 'a returned payout line is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.returned_at IS NOT NULL THEN
                SELECT * INTO b FROM public.{_B} WHERE tenant_id = NEW.tenant_id AND id = NEW.batch_id;
                IF NOT FOUND OR b.status IS DISTINCT FROM 'recorded' OR b.kind IS DISTINCT FROM 'net_pay' THEN
                    RAISE EXCEPTION 'only a line of a recorded net-pay batch can be returned'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.return_bank_account_id IS DISTINCT FROM b.bank_account_id THEN
                    RAISE EXCEPTION 'the return must name the batch bank account'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF {returner_condition}NEW.returned_by_party_id IN (
                    b.prepared_by_party_id, b.approved_by_party_id, b.file_issued_by_party_id, b.recorded_by_party_id
                ) THEN
                    RAISE EXCEPTION 'the returner must be a different person than the batch parties'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """  # noqa: S608


def upgrade() -> None:
    op.create_table(
        _POLICY,
        sa.Column("tenant_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("dual_control", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("set_by", sa.String(120), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("evidence_ref", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("length(btrim(set_by)) > 0", name=f"ck_{_POLICY}_set_by"),
        sa.CheckConstraint("length(btrim(evidence_ref)) > 0", name=f"ck_{_POLICY}_evidence"),
    )
    op.execute(f"ALTER TABLE public.{_POLICY} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{_POLICY} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY}_tenant_isolation ON public.{_POLICY} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    # Runtime reads the policy; only the operator CLI (owner identity) writes it. No UPDATE/DELETE/TRUNCATE.
    op.execute(f"GRANT SELECT ON public.{_POLICY} TO app_user")

    # Existing rows stay strict: the column default is true.
    for table in _TABLES:
        op.add_column(table, sa.Column("dual_control", sa.Boolean(), nullable=False, server_default=sa.true()))
    for table, name, predicate, integrity in _RULES:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"NOT dual_control OR ({predicate})")
        if integrity is not None:
            op.create_check_constraint(f"{name}_integrity", table, integrity)

    op.execute(_return_guard("NEW.dual_control AND "))

    op.execute(
        f"""
        CREATE FUNCTION {_MJ_FREEZE_FN}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.dual_control IS DISTINCT FROM OLD.dual_control THEN
                RAISE EXCEPTION 'the dual-control stamp of a manual journal is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {_MJ}_dual_control_freeze BEFORE UPDATE ON public.{_MJ} "
        f"FOR EACH ROW EXECUTE FUNCTION {_MJ_FREEZE_FN}()"
    )


def downgrade() -> None:
    # Restoring the strict CHECKs would be unsound over rows recorded with the policy off, and the policy
    # rows are governance evidence: refuse rather than discard either.
    bind = op.get_bind()
    for table in _TABLES:
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{table} WHERE NOT dual_control)")).scalar():  # noqa: S608
            raise RuntimeError(f"refusing to downgrade: {table} holds rows recorded with dual control off")
    if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{_POLICY})")).scalar():  # noqa: S608
        raise RuntimeError(f"refusing to drop {_POLICY}: policy rows exist")
    op.execute(f"DROP TRIGGER IF EXISTS {_MJ}_dual_control_freeze ON public.{_MJ}")
    op.execute(f"DROP FUNCTION IF EXISTS {_MJ_FREEZE_FN}()")
    op.execute(_return_guard(""))
    for table, name, predicate, integrity in reversed(_RULES):
        if integrity is not None:
            op.drop_constraint(f"{name}_integrity", table, type_="check")
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, predicate)
    for table in reversed(_TABLES):
        op.drop_column(table, "dual_control")
    op.drop_table(_POLICY)
