"""Test helper: record the per-tenant dual-control policy (D-9) the way the operator CLI would.

The default is OFF (no row). Existing separation-of-duties tests turn it ON for their tenant; the new
policy-off tests leave it unset or set it False.
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.governance.models import GovernancePolicy


def set_dual_control(db: Session, tenant_id: UUID, on: bool = True) -> None:
    """Upsert the tenant policy on an open session (flushes; caller commits as it already does)."""
    row = db.get(GovernancePolicy, tenant_id)
    if row is None:
        db.add(GovernancePolicy(tenant_id=tenant_id, dual_control=on, set_by="test-operator", evidence_ref="test"))
    else:
        row.dual_control = on
    db.flush()


def set_dual_control_engine(engine: sa.Engine, tenant_id: UUID, on: bool = True) -> None:
    with Session(engine) as db, db.begin():
        set_dual_control(db, tenant_id, on)
