"""Roles and permissions for the finance/payroll workflows, declared once.

Rules (Michael W14, register v1277): ``admin`` and ``staff`` receive NO new
permission. Every new permission names its own role in ``default_roles`` (the
kernel's compatibility-window binding), so the default ``("admin",)`` can never
leak in; approval, finalize and payout powers exist only for the named roles.
Roles carry no holders until named parties are granted them (``access_cli``);
every new route is dormant until then. Roles are created empty per tenant.
"""

from __future__ import annotations

from typing import Final

from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.permission_provisioning import RoleDefinition, RoleGrant, RoleGrantProfile
from dotmac_kernel.permissions import PermissionSpec

from erp_v2.web.payroll_routes import PAYROLL_MANAGE_PERMISSION
from erp_v2.web.revenue_routes import WRITE_PERMISSION
from erp_v2.web.routes import ACCESS_PERMISSION

PROFILE_CODE: Final = "erp.web"
PROFILE_VERSION: Final = 3

# Roles that exist today and keep exactly their current grants.
LEGACY_ROLES: Final = ("admin", "staff")
NEW_ROLES: Final = (
    "finance_preparer",
    "finance_approver",
    "treasury",
    "payroll_approver",
    "payroll_finalizer",
    "bank_reconciler",
)

# code -> (description, holding role). One declaration; the specs, role grants
# and tests all derive from this table.
_NEW_PERMISSIONS: Final[dict[str, tuple[str, str]]] = {
    "erp.finance.journals.prepare": ("Prepare manual journals and request posting.", "finance_preparer"),
    "erp.finance.supplier_payments.prepare": ("Prepare supplier payment proposals.", "finance_preparer"),
    "erp.finance.journals.approve": ("Approve and post or reverse manual journals.", "finance_approver"),
    "erp.finance.periods.manage": ("Open, soft-close and lock fiscal periods.", "finance_approver"),
    "erp.finance.supplier_invoices.approve": (
        "Approve supplier invoices and post them to the ledger.",
        "finance_approver",
    ),
    "erp.finance.supplier_payments.approve": ("Approve supplier payment proposals.", "finance_approver"),
    "erp.banking.reconciliation.approve": ("Approve bank reconciliations.", "finance_approver"),
    "erp.treasury.supplier_payments.record": ("Record supplier payments as made at the bank.", "treasury"),
    "erp.treasury.disbursement_files.generate": ("Generate bank upload files for payments and payouts.", "treasury"),
    "erp.treasury.payroll_payouts.record": ("Record payroll payouts and statutory remittances.", "treasury"),
    "erp.payroll.runs.approve": ("Approve calculated payroll runs.", "payroll_approver"),
    "erp.payroll.runs.finalize": ("Finalize approved payroll runs and post them.", "payroll_finalizer"),
    "erp.banking.statements.import": ("Import bank statements.", "bank_reconciler"),
    "erp.banking.reconciliation.prepare": ("Match and prepare bank reconciliations.", "bank_reconciler"),
}

NEW_PERMISSION_CODES: Final = frozenset(_NEW_PERMISSIONS)

PERMISSION_SPECS: Final = tuple(
    PermissionSpec(code=code, description=description, default_roles=(role,))
    for code, (description, role) in _NEW_PERMISSIONS.items()
)

ROLE_DEFINITIONS: Final = (
    *(RoleDefinition(code) for code in LEGACY_ROLES),
    *(RoleDefinition(code, create_if_missing=True) for code in NEW_ROLES),
)

_LEGACY_GRANTS: Final = (
    RoleGrant("admin", ACCESS_PERMISSION),
    RoleGrant("admin", WRITE_PERMISSION),
    RoleGrant("admin", PAYROLL_MANAGE_PERMISSION),
    RoleGrant("staff", ACCESS_PERMISSION),
)
_NEW_GRANTS: Final = (
    # Portal entry (erp.web.access) is NOT granted to the new roles: holders
    # must already be staff/admin (access_cli refuses otherwise), so no access
    # is widened. The profile therefore matches runtime authorization, which
    # uses each PermissionSpec.default_roles.
    *(RoleGrant(role, code) for code, (_, role) in _NEW_PERMISSIONS.items()),
)

ROLE_GRANT_PROFILES: Final = (
    RoleGrantProfile(code=PROFILE_CODE, version=PROFILE_VERSION, grants=_LEGACY_GRANTS + _NEW_GRANTS),
)

# Role pairs one person must not hold together (approve-and-pay, pay-and-reconcile,
# prepare-and-approve, approve-and-finalize). Overridable only by an explicit
# ``sod_exceptions`` entry inside the sha-bound allowlist (access_cli).
SOD_CONFLICT_PAIRS: Final = frozenset(
    frozenset(pair)
    for pair in (
        ("finance_preparer", "finance_approver"),
        ("payroll_approver", "payroll_finalizer"),
        ("finance_approver", "treasury"),
        ("treasury", "bank_reconciler"),
    )
)

AUDIT_ROLE_GRANTED: Final = "erp.access.role_granted"

access_feature: Final = FeatureManifest(
    name="erp_access", permissions=PERMISSION_SPECS, audit_actions=(AUDIT_ROLE_GRANTED,)
)
