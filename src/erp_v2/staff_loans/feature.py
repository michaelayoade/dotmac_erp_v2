"""Feature manifest for the staff-loan entries: audit actions only (service-level flow, no routes yet)."""

from __future__ import annotations

from dotmac_kernel.features import FeatureManifest

from erp_v2.staff_loans import service

staff_loans_feature = FeatureManifest(name="erp_staff_loans", audit_actions=service.AUDIT_ACTIONS)
