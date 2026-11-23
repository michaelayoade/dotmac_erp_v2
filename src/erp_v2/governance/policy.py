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
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from erp_v2.governance.models import GovernancePolicy


class GovernancePolicyError(RuntimeError):
    """The policy cannot be read safely for this tenant (fail closed; never read as OFF)."""


_BYPASS_KEY = "erp_governance.role_bypasses_rls"


def _session_tenant_guard(db: Session, tenant_id: UUID) -> None:
    """On PostgreSQL the row-level-security tenant setting must be this tenant.

    Under RLS a policy row of another tenant is invisible, so a mismatched or missing setting would read as
    "no row", that is OFF. That must never happen: refuse. A role that bypasses RLS (the operator CLI, the
    importer, owner sessions) sees the real row whatever the setting is, so only a setting that is present
    and different is refused for it.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    observed = db.scalar(text("SELECT public.app_current_tenant_id()"))
    if observed is not None and str(observed) != str(tenant_id):
        raise GovernancePolicyError("the session tenant differs from the tenant whose policy is read")
    if observed is None:
        bypass = db.info.get(_BYPASS_KEY)
        if bypass is None:
            bypass = bool(db.scalar(text("SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user")))
            db.info[_BYPASS_KEY] = bypass
        if not bypass:
            raise GovernancePolicyError("the session has no tenant setting; the policy cannot be read safely")


def dual_control(db: Session, tenant_id: UUID) -> bool:
    _session_tenant_guard(db, tenant_id)
    value = db.scalar(select(GovernancePolicy.dual_control).where(GovernancePolicy.tenant_id == tenant_id))
    return bool(value)


def separation_of_duties(db: Session, tenant_id: UUID) -> bool:
    """The value ERP passes as ``separation_of_duties=`` to dotmac-payroll and dotmac-payables (D-9).

    The same tenant policy as ``dual_control``: OFF unless the operator CLI turned it ON, and a failed read
    refuses (never reads as OFF). Every ERP call into those Starter functions must source the keyword from here
    (``tests/test_separation_call_sites.py`` fails any call that does not).
    """
    return dual_control(db, tenant_id)


AUDIT_DUAL_CONTROL_SET: Final = "erp.governance.dual_control_set"

governance_feature: Final = FeatureManifest(name="erp_governance", audit_actions=(AUDIT_DUAL_CONTROL_SET,))
