"""Audit vocabulary of the B1b recognize-and-post operator command."""

from __future__ import annotations

from typing import Final

from dotmac_kernel.features import FeatureManifest

AUDIT_RECOGNIZED_POSTED: Final = "erp.finance.supplier_invoice.legacy_recognized_posted"
SOURCE_APPLICATION: Final = "dotmac_erp"

legacy_recognize_feature = FeatureManifest(name="erp_legacy_recognize", audit_actions=(AUDIT_RECOGNIZED_POSTED,))
