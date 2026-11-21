"""Audit vocabulary of the claims opening (one event per apply: the reclass journal is the entity)."""

from __future__ import annotations

from typing import Final

from dotmac_kernel.features import FeatureManifest

AUDIT_APPLIED: Final = "erp.claims_opening.applied"
AUDIT_REPLAYED: Final = "erp.claims_opening.replayed"
SOURCE_APPLICATION: Final = "dotmac_erp"

claims_opening_feature = FeatureManifest(name="erp_claims_opening", audit_actions=(AUDIT_APPLIED, AUDIT_REPLAYED))


def install() -> None:
    """Install the audit action registry (the assembled modules plus this command's own vocabulary), once."""
    from dotmac_kernel.audit_actions import (
        AuditActionRegistry,
        AuditActionsNotInstalledError,
        active_audit_actions,
        install_audit_actions,
    )

    try:
        active_audit_actions()
    except AuditActionsNotInstalledError:
        from erp_v2.assembly import assembly

        install_audit_actions(AuditActionRegistry.from_manifests([*assembly.modules, claims_opening_feature]))
