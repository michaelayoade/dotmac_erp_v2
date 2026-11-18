"""Statutory payees: widen the payee kind CHECK to include ``statutory`` (V3 for B5 statutory remittances).

Additive: ``ck_payee_bank_instructions_kind`` becomes ``payee_kind IN ('supplier', 'employee', 'statutory')``.
Existing rows all satisfy the wider predicate. A statutory instruction is keyed by the payroll external
beneficiary ref (for example ``dest:PENSION_ER``) and follows the B8 lifecycle and the per-tenant
dual-control policy unchanged (no other constraint, grant or trigger changes).

Downgrade refuses while any statutory instruction exists (it would violate the narrower CHECK).

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


def upgrade() -> None:
    op.drop_constraint(_CK, _TABLE, type_="check")
    op.create_check_constraint(_CK, _TABLE, "payee_kind IN ('supplier', 'employee', 'statutory')")


def downgrade() -> None:
    bind = op.get_bind()
    found = bind.execute(sa.text("SELECT 1 FROM payee_bank_instructions WHERE payee_kind = 'statutory' LIMIT 1"))
    if found.first() is not None:
        raise RuntimeError("refusing to downgrade: statutory payee instructions exist")
    op.drop_constraint(_CK, _TABLE, type_="check")
    op.create_check_constraint(_CK, _TABLE, "payee_kind IN ('supplier', 'employee')")
