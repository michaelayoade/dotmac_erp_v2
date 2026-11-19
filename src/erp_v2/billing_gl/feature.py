"""Feature manifest for the live billing -> GL mapper: audit actions and the ``billing_gl`` settings domain."""

from __future__ import annotations

from typing import Final

from dotmac_kernel.features import FeatureManifest

from erp_v2.billing_gl import settings

AUDIT_RUN: Final = "erp.billing_gl.run_completed"
AUDIT_ALARM: Final = "erp.billing_gl.integrity_alarm"
AUDIT_ACTIONS: Final = (AUDIT_RUN, AUDIT_ALARM)

billing_gl_feature = FeatureManifest(
    name="erp_billing_gl", audit_actions=AUDIT_ACTIONS, setting_domains=(str(settings.DOMAIN),)
)
