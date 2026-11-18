"""Audit vocabulary of the legacy AP operator commands (B1b recognize-and-post, settled admission)."""

from __future__ import annotations

from typing import Final

from dotmac_kernel.features import FeatureManifest

AUDIT_RECOGNIZED_POSTED: Final = "erp.finance.supplier_invoice.legacy_recognized_posted"
AUDIT_SETTLED_ADMITTED: Final = "erp.finance.supplier_invoice.legacy_settled_admitted"
AUDIT_REVIEW_RECORDED: Final = "erp.finance.admission_review.recorded"
SOURCE_APPLICATION: Final = "dotmac_erp"

legacy_recognize_feature = FeatureManifest(
    name="erp_legacy_recognize",
    audit_actions=(AUDIT_RECOGNIZED_POSTED, AUDIT_SETTLED_ADMITTED, AUDIT_REVIEW_RECORDED),
)
