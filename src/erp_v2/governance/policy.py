"""Read side of the per-tenant dual-control policy.

``dual_control(db, tenant_id)`` is True only when the operator CLI recorded ``dual_control = true`` for the
tenant. No row, or ``false``, reads as False (Michael's default: no compulsory two-person).

Where a governed row exists (manual journals, payee instructions, reconciliations, payout batches and lines,
cash GL re-points) the policy is stamped on the row at creation (``dual_control`` column, DB default true so
rows that predate e2_0022 stay strict) and every later check reads the row, never the live policy. Where no
ERP row exists (B1 approval, B3 payments, B4 runs, role grants) the tenant policy is read at the moment of the
check.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from dotmac_kernel.features import FeatureManifest
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.governance.models import GovernancePolicy


def dual_control(db: Session, tenant_id: UUID) -> bool:
    value = db.scalar(select(GovernancePolicy.dual_control).where(GovernancePolicy.tenant_id == tenant_id))
    return bool(value)


AUDIT_DUAL_CONTROL_SET: Final = "erp.governance.dual_control_set"

governance_feature: Final = FeatureManifest(name="erp_governance", audit_actions=(AUDIT_DUAL_CONTROL_SET,))
