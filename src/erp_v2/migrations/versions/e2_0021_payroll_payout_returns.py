"""Returned payroll payout lines (payroll a5 settlement reversal; B5 return action).

Additive: ten nullable columns on ``erp_payroll_payout_lines``, two partial unique indexes and one CHECK,
plus a guard trigger. No existing column, grant, policy or row changes. The runtime role gains UPDATE on
the new columns only; ``active`` was already updatable. A trigger makes the return record write-once, and
at the moment of writing it re-checks in the database that the batch is a RECORDED net-pay batch and that
the returner is none of its preparer, approver, file issuer or recorder.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.payroll_payouts.models import LINE_RETURN_COLUMNS

revision = "e2_0021_payroll_payout_returns"
down_revision = "e2_0020_cash_account_repoints"
branch_labels = None
depends_on = None

_L = "erp_payroll_payout_lines"
_B = "erp_payroll_payout_batches"
_FN = f"public.{_L}_return_guard"


def upgrade() -> None:
    for name, column_type in (
        ("returned_at", sa.DateTime(timezone=True)),
        ("returned_by_party_id", sa.Uuid()),
        ("return_reference", sa.String(255)),
        ("return_reason", sa.String(500)),
        ("return_value_date", sa.Date()),
        ("return_bank_account_id", sa.Uuid()),
        ("reversal_id", sa.Uuid()),
        ("reversal_journal_id", sa.Uuid()),
        ("reversal_journal_number", sa.String(60)),
        ("reversal_fingerprint", sa.String(64)),
    ):
        op.add_column(_L, sa.Column(name, column_type, nullable=True))
    op.create_check_constraint(
        f"ck_{_L}_returned",
        _L,
        "returned_at IS NULL OR (active = false AND settlement_id IS NOT NULL AND journal_id IS NOT NULL "
        "AND returned_by_party_id IS NOT NULL AND return_reference IS NOT NULL AND return_reason IS NOT NULL "
        "AND return_value_date IS NOT NULL AND return_bank_account_id IS NOT NULL AND reversal_id IS NOT NULL "
        "AND reversal_journal_id IS NOT NULL AND reversal_fingerprint IS NOT NULL)",
    )
    op.create_index(
        f"uq_{_L}_return_reference",
        _L,
        ["tenant_id", "return_bank_account_id", "return_reference"],
        unique=True,
        postgresql_where=sa.text("returned_at IS NOT NULL"),
    )
    op.create_index(
        f"uq_{_L}_reversal",
        _L,
        ["tenant_id", "reversal_id"],
        unique=True,
        postgresql_where=sa.text("returned_at IS NOT NULL"),
    )
    op.execute(
        f"""
        CREATE FUNCTION {_FN}() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE b public.{_B}%ROWTYPE;
        BEGIN
            IF OLD.returned_at IS NOT NULL THEN
                RAISE EXCEPTION 'a returned payout line is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.returned_at IS NOT NULL THEN
                SELECT * INTO b FROM public.{_B} WHERE tenant_id = NEW.tenant_id AND id = NEW.batch_id;
                IF b.status <> 'recorded' OR b.kind <> 'net_pay' THEN
                    RAISE EXCEPTION 'only a line of a recorded net-pay batch can be returned'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.returned_by_party_id IN (
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
    )
    op.execute(f"CREATE TRIGGER {_L}_return_guard BEFORE UPDATE ON public.{_L} FOR EACH ROW EXECUTE FUNCTION {_FN}()")
    op.execute(f"GRANT UPDATE ({', '.join(LINE_RETURN_COLUMNS)}) ON public.{_L} TO app_user")


def downgrade() -> None:
    # A return record is the evidence that a salary credit came back and the ledger was posted: never drop it.
    used = (
        op.get_bind()
        .execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{_L} WHERE returned_at IS NOT NULL)")  # noqa: S608
        )
        .scalar()
    )
    if used:
        raise RuntimeError(f"refusing to drop the return columns of {_L}: returned lines exist")
    op.execute(f"DROP TRIGGER IF EXISTS {_L}_return_guard ON public.{_L}")
    op.execute(f"DROP FUNCTION IF EXISTS {_FN}()")
    op.drop_index(f"uq_{_L}_reversal", table_name=_L)
    op.drop_index(f"uq_{_L}_return_reference", table_name=_L)
    op.drop_constraint(f"ck_{_L}_returned", _L, type_="check")
    for name in reversed(LINE_RETURN_COLUMNS):
        op.drop_column(_L, name)
