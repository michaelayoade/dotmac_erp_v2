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
PROFILE_VERSION: Final = 4

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

# Payee bank instruction permissions (B8, W10 two-person rule). Treasury holds the READ (masked)
# permission only: it can see which destination is approved but can never propose, approve,
# reject, revoke or see the full account number. Approval is finance_approver only, and role SoD
# (finance_approver + treasury, finance_preparer + finance_approver) keeps the proposer and the
# approver, and treasury and the approver, as different people.
PAYEE_BANK_READ: Final = "erp.payees.bank_details.read"
PAYEE_BANK_PROPOSE: Final = "erp.payees.bank_details.propose"
PAYEE_BANK_APPROVE: Final = "erp.payees.bank_details.approve"
PAYEE_BANK_REVOKE: Final = "erp.payees.bank_details.revoke"
PAYEE_BANK_READ_UNMASKED: Final = "erp.payees.bank_details.read_unmasked"
PAYEE_BANK_CODES: Final = (
    PAYEE_BANK_READ,
    PAYEE_BANK_PROPOSE,
    PAYEE_BANK_APPROVE,
    PAYEE_BANK_REVOKE,
    PAYEE_BANK_READ_UNMASKED,
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
    "erp.finance.payroll_payouts.prepare": (
        "Prepare payroll payout batches and statutory remittances.",
        "finance_preparer",
    ),
    "erp.finance.payroll_payouts.approve": ("Approve or cancel payroll payout batches.", "finance_approver"),
    "erp.treasury.supplier_payments.record": ("Record supplier payments as made at the bank.", "treasury"),
    "erp.treasury.disbursement_files.generate": ("Generate bank upload files for payments and payouts.", "treasury"),
    "erp.treasury.payroll_payouts.record": ("Record payroll payouts and statutory remittances.", "treasury"),
    "erp.payroll.runs.approve": ("Approve calculated payroll runs.", "payroll_approver"),
    "erp.payroll.runs.finalize": ("Finalize approved payroll runs and post them.", "payroll_finalizer"),
    "erp.banking.statements.import": ("Import bank statements.", "bank_reconciler"),
    "erp.banking.reconciliation.prepare": ("Match and prepare bank reconciliations.", "bank_reconciler"),
}

# B8: payee bank instruction permissions, code -> (description, holding roles).
_PAYEE_PERMISSIONS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    PAYEE_BANK_READ: (
        "View payee bank instructions with the account number masked.",
        ("finance_preparer", "finance_approver", "treasury"),
    ),
    PAYEE_BANK_PROPOSE: ("Propose a new or changed payee bank instruction.", ("finance_preparer",)),
    PAYEE_BANK_APPROVE: (
        "Approve or reject a payee bank instruction proposed by someone else.",
        ("finance_approver",),
    ),
    PAYEE_BANK_REVOKE: ("Revoke an approved payee bank instruction.", ("finance_approver",)),
    PAYEE_BANK_READ_UNMASKED: (
        "View a full payee account number (audited, reason required).",
        ("finance_approver",),
    ),
}

# Every new permission as (code, description, roles).
_ALL_NEW: Final = (
    *((code, description, (role,)) for code, (description, role) in _NEW_PERMISSIONS.items()),
    *((code, description, roles) for code, (description, roles) in _PAYEE_PERMISSIONS.items()),
)

NEW_PERMISSION_CODES: Final = frozenset(code for code, _, _ in _ALL_NEW)

PERMISSION_SPECS: Final = tuple(
    PermissionSpec(code=code, description=description, default_roles=roles) for code, description, roles in _ALL_NEW
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
    *(RoleGrant(role, code) for code, _, roles in _ALL_NEW for role in roles),
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

# Never waivable by an allowlist ``sod_exceptions`` entry (REVIEW-B8 R3, F7): whoever approves a
# payee destination or a payment must not also generate the bank file.
NON_WAIVABLE_SOD_PAIRS: Final = frozenset({frozenset(("finance_approver", "treasury"))})

AUDIT_ROLE_GRANTED: Final = "erp.access.role_granted"
AUDIT_ROLE_REVOKED: Final = "erp.access.role_revoked"

access_feature: Final = FeatureManifest(
    name="erp_access", permissions=PERMISSION_SPECS, audit_actions=(AUDIT_ROLE_GRANTED, AUDIT_ROLE_REVOKED)
)
