"""Statutory payees: widen the payee kind CHECK to include ``statutory`` (V3 for B5 statutory remittances).

Additive: ``ck_payee_bank_instructions_kind`` becomes ``payee_kind IN ('supplier', 'employee', 'statutory')``.
Existing rows all satisfy the wider predicate. A statutory instruction is keyed by the payroll external
beneficiary ref (for example ``dest:PENSION_ER``) and follows the B8 lifecycle and the per-tenant
dual-control policy unchanged (no other constraint, grant or trigger changes).

It also adds ``erp_payroll_payout_lines.payee_instruction_id`` (nullable): the approved payee instruction a line
was approved against, written once at approve (runtime UPDATE on that column only, plus a write-once trigger).
B5 record refuses with ``payee_instruction_changed`` if the current approved instruction differs.

Downgrade refuses while any statutory instruction exists or any payout line holds a pinned instruction id.

Revision chain: e2_0023 (staff loan entries) -> this revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0024_statutory_payees"
down_revision = "e2_0023_staff_loan_entries"
branch_labels = None
depends_on = None

_TABLE = "payee_bank_instructions"
_CK = "ck_payee_bank_instructions_kind"
_L = "erp_payroll_payout_lines"
_FN = "erp_payout_line_instruction_guard"


def upgrade() -> None:
    op.drop_constraint(_CK, _TABLE, type_="check")
    op.create_check_constraint(_CK, _TABLE, "payee_kind IN ('supplier', 'employee', 'statutory')")
    op.add_column(_L, sa.Column("payee_instruction_id", sa.Uuid(), nullable=True))
    op.execute(
        f"""
        CREATE FUNCTION public.{_FN}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.payee_instruction_id IS NOT NULL
               AND NEW.payee_instruction_id IS DISTINCT FROM OLD.payee_instruction_id THEN
                RAISE EXCEPTION 'the approved payee instruction of a payout line is write-once'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {_L}_instruction_guard BEFORE UPDATE ON public.{_L} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{_FN}()"
    )
    op.execute(f"GRANT UPDATE (payee_instruction_id) ON public.{_L} TO app_user")


def downgrade() -> None:
    bind = op.get_bind()
    pinned = bind.execute(
        sa.text("SELECT 1 FROM erp_payroll_payout_lines WHERE payee_instruction_id IS NOT NULL LIMIT 1")
    )
    if pinned.first() is not None:
        raise RuntimeError("refusing to downgrade: payout lines hold pinned payee instruction ids")
    found = bind.execute(sa.text("SELECT 1 FROM payee_bank_instructions WHERE payee_kind = 'statutory' LIMIT 1"))
    if found.first() is not None:
        raise RuntimeError("refusing to downgrade: statutory payee instructions exist")
    op.execute(f"DROP TRIGGER {_L}_instruction_guard ON public.{_L}")
    op.execute(f"DROP FUNCTION public.{_FN}()")
    op.drop_column(_L, "payee_instruction_id")
    op.drop_constraint(_CK, _TABLE, type_="check")
    op.create_check_constraint(_CK, _TABLE, "payee_kind IN ('supplier', 'employee')")
