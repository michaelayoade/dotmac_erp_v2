"""Code-declared ledger account roles shared by the supplier, journal and payment workflows.

No setting and no migration: the codes below are Finance-declared constants (confirmed 2026-11-17,
decision D-1) and are pinned by tests to the other places that already declare them. The resolvers
are read-only and tenant-filtered; they never create an account.

* A control account (2000 AP, 2040 payroll payable, 1410 staff loans, 1400 AR, 2045 employee claims, and the AR
  prepayment liability account named by ``ar.prepayment_liability_account``) changes only through a document of its
  owning subledger, or (2000/2040/1410 only) through the system-built correction of a proven ledger fault.
* A supplier-invoice tax line may only use an input-tax account (1455) that is not the line's own
  posting account.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final
from uuid import UUID

from dotmac_accounting.models import Account, JournalEntry, JournalLine, JournalStatus
from dotmac_kernel.setting_scopes import SettingScope
from dotmac_kernel.settings_resolver import SettingSpec, StrictSettingResolutionError, resolve_strict
from sqlalchemy import select
from sqlalchemy.orm import Session

# Imported here, not lazily: importing it registers the ``ar.*`` setting specs, and that must happen before any module
# declares a stand-in for them (a late registration would be a DuplicateSettingSpecError).
from erp_v2.ledger_settings import (
    PREPAID_FUNDING_SPEC,
    PREPAYMENT_DEFAULT,
    PREPAYMENT_SPEC,
    account_ref_matches,
)

AP_CONTROL_CODE: Final = "2000"
PAYROLL_PAYABLE_CODE: Final = "2040"
STAFF_LOAN_CODE: Final = "1410"
AR_CONTROL_CODE: Final = "1400"
EMPLOYEE_CLAIMS_CODE: Final = "2045"
# S9b (plan 6-C2): 1400 and 2045 join the fixed control codes; the AR prepayment account is a runtime setting, so it
# joins by resolution (``prepayment_account_ids``), not by a constant.
CONTROL_ACCOUNT_CODES: Final = frozenset(
    {AP_CONTROL_CODE, PAYROLL_PAYABLE_CODE, STAFF_LOAN_CODE, AR_CONTROL_CODE, EMPLOYEE_CLAIMS_CODE}
)
# The system-built ledger-fault door (journals) is NOT widened by the S9b tightening: it stays on the original three.
LEDGER_FAULT_CONTROL_CODES: Final = frozenset({AP_CONTROL_CODE, PAYROLL_PAYABLE_CODE, STAFF_LOAN_CODE})
# The flip-gate control checks (B-9, section 12.4). A code constant, not a setting and not a per-run declaration, so a
# run cannot drop an account to pass; each signed bridge records the lists it checked. Changing a list is a code change.
GATE_TIE_CODES: Final = ("1400", "1410", "2000", "2040", "2045")  # GL must equal the v2 owner subledger
# A tie whose GL account is resolved from a setting, not a code: the AR prepayment account (== -(available credit +
# prepaid funding) of billing R1). Recorded in the signed bridge body beside GATE_TIE_CODES.
GATE_TIE_RESOLVED: Final = ("prepayment",)
REPORT_ZERO_CODES: Final = ("2030",)  # retired (D-EX-9): REPORTED, not a gate (as_is_v1): net over all periods
REPORT_WHT_CODES: Final = ("2110",)  # REPORTED, not a gate (as_is_v1): GL against the WHT ledger
INVOICE_TAX_ACCOUNT_CODES: Final = frozenset({"1455"})
INPUT_VAT_RELEASE_CODES: Final = frozenset({"1440"})  # where released deferred input VAT is claimed (a6)


class LedgerRoleError(ValueError):
    """A declared account role does not resolve for this tenant; carries a stable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def tax_role_account_ids(db: Session, tenant_id: UUID) -> set[str]:
    """Accounts that hold VAT (input-tax 1455, release 1440, any account named by a declared deferral policy):
    never an invoice or credit line's own posting account."""
    ids = {
        str(i)
        for i in db.scalars(
            select(Account.id).where(
                Account.tenant_id == tenant_id, Account.code.in_(INVOICE_TAX_ACCOUNT_CODES | INPUT_VAT_RELEASE_CODES)
            )
        )
    }
    try:
        from dotmac_payables.models import TaxDeferralPolicy  # a6 only
    except ImportError:
        return ids
    for policy in db.scalars(select(TaxDeferralPolicy).where(TaxDeferralPolicy.tenant_id == tenant_id)):
        ids.update((str(policy.deferral_account_ref), str(policy.release_account_ref)))
    return ids


def _setting_account_ids(db: Session, tenant_id: UUID, spec: SettingSpec, default: str | None) -> frozenset[UUID]:
    try:
        value = resolve_strict(db, spec, scope=SettingScope.tenant(tenant_id))
    except StrictSettingResolutionError:
        value = default  # an invalid stored value falls back to the registered default reference (fail-closed)
    return frozenset(account_ref_matches(db, tenant_id, value))


def prepayment_account_ids(db: Session, tenant_id: UUID) -> frozenset[UUID]:
    """Every account ``ar.prepayment_liability_account`` can name, by the ONE shared resolver (id, else code, else every
    exact-name match; any status). A name shared by several accounts protects ALL of them even though the mapper then
    reads the setting as unset. Nothing resolving protects nothing."""
    return _setting_account_ids(db, tenant_id, PREPAYMENT_SPEC, PREPAYMENT_DEFAULT)


def prepaid_funding_account_ids(db: Session, tenant_id: UUID) -> frozenset[UUID]:
    """The same for ``ar.prepaid_funding_account`` (part of the credit tie; unset by default)."""
    return _setting_account_ids(db, tenant_id, PREPAID_FUNDING_SPEC, None)


def t1_credit_account_ids(db: Session, tenant_id: UUID) -> frozenset[UUID]:
    """The credit account of every POSTED T1 journal: once T1 has funded an account, re-pointing the setting cannot
    make that balance bare-postable. Identity-bound (owner/kind), no setting and no table."""
    from erp_v2.holdings.models import KIND_T1_POST, OWNER  # lazy: holdings imports this module

    return frozenset(
        db.scalars(
            select(JournalLine.account_id)
            .join(
                JournalEntry,
                (JournalEntry.tenant_id == JournalLine.tenant_id) & (JournalEntry.id == JournalLine.journal_id),
            )
            .where(
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.source_owner == OWNER,
                JournalEntry.source_document_kind == KIND_T1_POST,
                JournalEntry.status == JournalStatus.POSTED,
                JournalLine.credit_functional > 0,
            )
        )
    )


def control_account_ids(db: Session, tenant_id: UUID) -> frozenset[UUID]:
    """Every control account of this tenant: the fixed codes, both ``ar.*`` accounts, and every account a posted T1
    journal credited."""
    fixed = db.scalars(
        select(Account.id).where(Account.tenant_id == tenant_id, Account.code.in_(CONTROL_ACCOUNT_CODES))
    )
    return (
        frozenset(fixed)
        | prepayment_account_ids(db, tenant_id)
        | prepaid_funding_account_ids(db, tenant_id)
        | t1_credit_account_ids(db, tenant_id)
    )


def account_id_for_code(db: Session, tenant_id: UUID, code: str) -> UUID | None:
    """The tenant account with this code, in any status (codes are unique per tenant)."""
    return db.scalar(select(Account.id).where(Account.tenant_id == tenant_id, Account.code == code))


def ap_control_account_id(db: Session, tenant_id: UUID) -> UUID:
    found = account_id_for_code(db, tenant_id, AP_CONTROL_CODE)
    if found is None:
        raise LedgerRoleError("ap_control_unavailable", "The AP control account is not set up for this tenant")
    return found


def invoice_tax_account_ids(db: Session, tenant_id: UUID) -> frozenset[UUID]:
    found = frozenset(
        db.scalars(
            select(Account.id).where(Account.tenant_id == tenant_id, Account.code.in_(INVOICE_TAX_ACCOUNT_CODES))
        )
    )
    if not found:
        raise LedgerRoleError("input_tax_account_unavailable", "The input-tax account is not set up for this tenant")
    return found


def control_codes_among(db: Session, tenant_id: UUID, account_ids: Iterable[UUID]) -> set[str]:
    """The control-account codes (if any) among these account ids."""
    ids = set(account_ids)
    if not ids:
        return set()
    control = control_account_ids(db, tenant_id)
    return set(db.scalars(select(Account.code).where(Account.tenant_id == tenant_id, Account.id.in_(ids & control))))


def _is_uuid(value: object) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def same_account(ref: object, account_id: UUID) -> bool:
    """Compare an account reference to an id by UUID value; unparsable or missing is False."""
    if ref is None:
        return False
    try:
        return UUID(str(ref)) == account_id
    except ValueError:
        return False


def invoice_account_violation(
    db: Session,
    tenant_id: UUID,
    *,
    liability_ref: object,
    lines: Iterable[tuple[object, object, object]],
) -> tuple[str, str] | None:
    """The first V1/V2 refusal for a supplier invoice's accounts, as ``(code, message)``, or None.

    ``lines`` are ``(posting_account_ref, tax_amount, tax_account_ref)``. The liability must be the AP
    control account (2000). A taxed line's tax account must not be the line's own posting account and
    must be a declared input-tax account (1455); that also blocks WHT payable, 1440, 2120 and expense
    accounts. No line or tax account may be a control account (2000/2040/1410/1400/2045 or the prepayment
    account): the liability pin alone
    would still let ``Dr 1410 / Cr 2000`` through as an invoice line. Read-only; nothing is created.
    """
    try:
        ap_control = ap_control_account_id(db, tenant_id)
    except LedgerRoleError as exc:
        return exc.code, str(exc)
    if not same_account(liability_ref, ap_control):
        return "liability_not_ap_control", "A supplier invoice must be booked to the AP control account (2000)"
    rows = list(lines)
    control_ids = set(control_account_ids(db, tenant_id))
    for number, (posting, tax, tax_ref) in enumerate(rows, start=1):
        refs = [posting, tax_ref] if tax is not None and tax > 0 else [posting]
        for ref in refs:
            if any(same_account(ref, control_id) for control_id in control_ids):
                return (
                    "line_account_is_control",
                    f"Line {number}: a control account (2000/2040/1410/1400/2045/prepayment) cannot be a line or "
                    "tax account; "
                    "correct it through the owning subledger document",
                )
    tax_roles = tax_role_account_ids(db, tenant_id)
    for number, (posting, _tax, _tax_ref) in enumerate(rows, start=1):
        if any(same_account(posting, UUID(t)) for t in tax_roles if _is_uuid(t)) or str(posting) in tax_roles:
            return (
                "line_account_is_tax_account",
                f"Line {number}: an input-tax or release account (1455/1440) cannot be a line's own posting account",
            )
    taxed = [(posting, tax_ref) for posting, tax, tax_ref in rows if tax is not None and tax > 0]
    if not taxed:
        return None
    allowed: frozenset[UUID] | None = None
    for number, (posting, tax_ref) in enumerate(taxed, start=1):
        try:
            tax_id = UUID(str(tax_ref))
        except ValueError:
            return "tax_account_not_input_tax", f"Taxed line {number}: the tax account is not an input-tax account"
        if same_account(posting, tax_id):
            return "tax_account_is_line_account", f"Taxed line {number}: the tax account is the line's own account"
        if allowed is None:
            try:
                allowed = invoice_tax_account_ids(db, tenant_id)
            except LedgerRoleError as exc:
                return exc.code, str(exc)
        if tax_id not in allowed:
            return "tax_account_not_input_tax", f"Taxed line {number}: the tax account is not an input-tax account"
    return None
