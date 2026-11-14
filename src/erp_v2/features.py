"""Ordered ERP feature registry: later PRs add one entry here, not edits in assembly.py.

Order is canonical (readiness first) and asserted by tests.
"""

from __future__ import annotations

from dotmac_kernel.features import FeatureManifest

from erp_v2.academy_sync.routes import academy_sync_feature
from erp_v2.access import access_feature
from erp_v2.banking.routes import banking_web_feature
from erp_v2.readiness import readiness_feature
from erp_v2.web.payroll_routes import payroll_web_feature
from erp_v2.web.revenue_routes import revenue_web_feature
from erp_v2.web.routes import erp_web_feature

ERP_FEATURES: tuple[FeatureManifest, ...] = (
    readiness_feature,
    academy_sync_feature,
    erp_web_feature,
    revenue_web_feature,
    payroll_web_feature,
    banking_web_feature,
    access_feature,
)
