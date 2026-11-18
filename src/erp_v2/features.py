"""Ordered ERP feature registry: later PRs add one entry here, not edits in assembly.py.

Order is canonical (readiness first) and asserted by tests.
"""

from __future__ import annotations

from dotmac_kernel.features import FeatureManifest

from erp_v2.academy_sync.routes import academy_sync_feature
from erp_v2.access import access_feature
from erp_v2.banking.recon_routes import banking_recon_feature
from erp_v2.banking.routes import banking_web_feature
from erp_v2.banking.statement_routes import banking_statements_feature
from erp_v2.cash_gl.routes import cash_gl_feature
from erp_v2.governance.policy import governance_feature
from erp_v2.journals.routes import journals_web_feature
from erp_v2.payees.routes import payee_bank_feature
from erp_v2.payroll_payouts.routes import payroll_payouts_feature
from erp_v2.readiness import readiness_feature
from erp_v2.staff_loans.feature import staff_loans_feature
from erp_v2.supplier_payments.routes import supplier_payments_feature
from erp_v2.web.legacy_reentry_routes import legacy_reentry_feature
from erp_v2.web.payroll_routes import payroll_web_feature
from erp_v2.web.payroll_run_routes import payroll_run_feature
from erp_v2.web.revenue_approval_routes import revenue_approval_feature
from erp_v2.web.revenue_routes import revenue_web_feature
from erp_v2.web.routes import erp_web_feature

ERP_FEATURES: tuple[FeatureManifest, ...] = (
    readiness_feature,
    academy_sync_feature,
    erp_web_feature,
    revenue_web_feature,
    revenue_approval_feature,
    legacy_reentry_feature,
    payroll_web_feature,
    payroll_run_feature,
    banking_web_feature,
    journals_web_feature,
    banking_statements_feature,
    banking_recon_feature,
    cash_gl_feature,
    payee_bank_feature,
    supplier_payments_feature,
    payroll_payouts_feature,
    staff_loans_feature,
    access_feature,
    governance_feature,
)
