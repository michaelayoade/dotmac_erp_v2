# ruff: noqa: E501
"""Holding configuration (B14.2): account keys from the run-spec, validated against the tenant chart.

Each ``migration.holding_account.<code>`` value is an account CODE or exact account NAME that must resolve to exactly
one active, postable account that is neither a control account nor a tax-role account. A holding account may be shared
only across the code groups ``SHARING`` allows; any other sharing refuses ``holding_account_invalid``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from dotmac_accounting.contracts import AccountKind
from dotmac_accounting.models import Account
from dotmac_kernel.setting_scopes import SettingScope
from dotmac_kernel.settings_resolver import StrictSettingResolutionError, resolve_strict
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.holdings.errors import refuse
from erp_v2.holdings.models import HOLDING_CODES, PREPAYMENT
from erp_v2.ledger_roles import CONTROL_ACCOUNT_CODES, tax_role_account_ids
from erp_v2.ledger_settings import PREPAID_FUNDING_SPEC, PREPAYMENT_SPEC

KEY_PREFIX: Final = "migration.holding_account."
# Control accounts the tie covers today (1400, 2045 and prepayment join CONTROL_ACCOUNT_CODES in S9, not here).
TIE_CONTROL_CODES: Final = frozenset(CONTROL_ACCOUNT_CODES | {"1400", "2045"})
DEFAULTS: Final = {
    "2000": "AP migration clearing",
    "1400": "AR migration clearing",
    PREPAYMENT: "AR migration clearing",
    "2040": "Employee liabilities migration clearing",
    "2045": "Employee liabilities migration clearing",
    "1410": "Staff-loan migration clearing",
}
# Allowed sharing: {1400, prepayment}; {2040, 2045}. Every other code owns its account.
SHARING: Final = (frozenset({"1400", PREPAYMENT}), frozenset({"2040", "2045"}))


@dataclass(frozen=True, slots=True)
class AccountRef:
    id: UUID
    code: str
    name: str


def resolve_account(db: Session, tenant: UUID, reference: object, *, what: str) -> AccountRef:
    """The one active, postable account whose code or exact name is ``reference``; anything else refuses."""
    if not isinstance(reference, str) or not reference.strip() or reference != reference.strip():
        refuse("holding_account_invalid", f"{what}: reference is not text")
    rows = list(
        db.scalars(
            select(Account).where(
                Account.tenant_id == tenant, (Account.code == reference) | (Account.name == reference)
            )
        )
    )
    # a code match wins over a name match (codes are unique; names are not)
    by_code = [a for a in rows if a.code == reference]
    chosen = by_code or rows
    if len(chosen) != 1:
        refuse("holding_account_invalid", f"{what}: does not resolve to exactly one account")
    a = chosen[0]
    if not a.is_active or not a.posting_allowed or a.kind != AccountKind.POSTING:
        refuse("holding_account_invalid", f"{what}: account is not active and postable")
    return AccountRef(a.id, a.code, a.name)


def control_account_id(db: Session, tenant: UUID, code: str) -> AccountRef:
    row = db.scalar(select(Account).where(Account.tenant_id == tenant, Account.code == code))
    if row is None:
        refuse("control_account_missing", code)
    return AccountRef(row.id, row.code, row.name)


def prepayment_account(db: Session, tenant: UUID) -> AccountRef:
    """The runtime ``ar.prepayment_liability_account`` (default "Customer prepayments"); it is the control account of the
    ``prepayment`` tie code."""
    try:
        value = resolve_strict(db, PREPAYMENT_SPEC, scope=SettingScope.tenant(tenant))
    except StrictSettingResolutionError:
        return refuse("prepayment_account_unset")
    if value is None:
        refuse("prepayment_account_unset")
    ref = resolve_account(db, tenant, value, what="ar.prepayment_liability_account")
    return ref


def prepaid_funding_account(db: Session, tenant: UUID) -> AccountRef | None:
    try:
        value = resolve_strict(db, PREPAID_FUNDING_SPEC, scope=SettingScope.tenant(tenant))
    except StrictSettingResolutionError:
        return None
    if value is None:
        return None
    return resolve_account(db, tenant, value, what="ar.prepaid_funding_account")


@dataclass(frozen=True, slots=True)
class HoldingConfig:
    accounts: dict[str, AccountRef]  # control code -> holding account
    writeoff_offset_codes: tuple[str, ...]

    def as_body(self) -> dict[str, Any]:
        return {
            "holding_accounts": {c: {"id": str(a.id), "code": a.code} for c, a in sorted(self.accounts.items())},
            "writeoff_offset_codes": list(self.writeoff_offset_codes),
        }


def resolve_config(db: Session, tenant: UUID, params: dict[str, Any]) -> HoldingConfig:
    """Resolve and validate the holding accounts from the run-spec ``params`` (keys ``migration.holding_account.<c>``)."""
    unknown = [k for k in params if k.startswith(KEY_PREFIX) and k[len(KEY_PREFIX) :] not in HOLDING_CODES]
    if unknown:
        refuse("holding_account_invalid", "unknown holding account key")
    tax_roles = tax_role_account_ids(db, tenant)
    resolved: dict[str, AccountRef] = {}
    for code in HOLDING_CODES:
        ref = resolve_account(db, tenant, params.get(f"{KEY_PREFIX}{code}", DEFAULTS[code]), what=f"{KEY_PREFIX}{code}")
        if ref.code in TIE_CONTROL_CODES or str(ref.id) in tax_roles:
            refuse("holding_account_invalid", f"{KEY_PREFIX}{code}: a control or tax-role account cannot hold")
        resolved[code] = ref
    prepayment = prepayment_account(db, tenant)
    owners: dict[UUID, set[str]] = {}
    for code, ref in resolved.items():
        owners.setdefault(ref.id, set()).add(code)
        if ref.id == prepayment.id:
            refuse("holding_account_invalid", "a holding account cannot be the prepayment account")
    for codes in owners.values():
        if len(codes) > 1 and not any(codes <= group for group in SHARING):
            refuse("holding_account_invalid", "holding account shared across codes that may not share")
    raw = params.get("migration.writeoff_offset_codes", [])
    if not isinstance(raw, list) or any(not isinstance(c, str) or not c for c in raw):
        refuse("holding_account_invalid", "migration.writeoff_offset_codes must be a list of codes")
    return HoldingConfig(resolved, tuple(sorted(set(raw))))
