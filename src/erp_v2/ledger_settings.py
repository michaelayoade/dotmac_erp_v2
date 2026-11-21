"""Runtime ledger settings registered by B-14 (B14.2): the AR prepayment account and the prepaid-funding account.

Both hold an ACCOUNT REFERENCE as text: an account code or an account name that resolves to exactly one active,
postable account (see ``erp_v2.holdings.config.resolve_account``). They are runtime kernel settings (changeable by
Finance in the UI), not gate criteria. B-15 reads the same keys by string.

* ``ar.prepayment_liability_account``: default "Customer prepayments" (the account B-10-lite creates).
* ``ar.prepaid_funding_account``: unset by default (T6: while unset, a prepaid-funding position blocks).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from dotmac_accounting.models import Account
from dotmac_kernel.settings_models import SettingDomain, SettingValueType
from dotmac_kernel.settings_resolver import SettingSpec, register_specs
from sqlalchemy import select
from sqlalchemy.orm import Session

LEDGER_POLICY_DOMAIN: Final = SettingDomain("accounting_policy")
PREPAYMENT_KEY: Final = "ar.prepayment_liability_account"
PREPAID_FUNDING_KEY: Final = "ar.prepaid_funding_account"
PREPAYMENT_DEFAULT: Final = "Customer prepayments"


def _account_ref(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 120:
        raise ValueError("Use an account code or an exact account name")


PREPAYMENT_SPEC: Final[SettingSpec[str | None]] = SettingSpec(
    domain=LEDGER_POLICY_DOMAIN,
    key=PREPAYMENT_KEY,
    value_type=SettingValueType.string,
    default=PREPAYMENT_DEFAULT,
    label="AR prepayment liability account",
    description="Account (code or exact name) holding customer prepayments and available credit.",
    required_at=None,
    inherits=False,
    validator=_account_ref,
)

PREPAID_FUNDING_SPEC: Final[SettingSpec[str | None]] = SettingSpec(
    domain=LEDGER_POLICY_DOMAIN,
    key=PREPAID_FUNDING_KEY,
    value_type=SettingValueType.string,
    default=None,
    label="AR prepaid funding account",
    description="Account (code or exact name) for prepaid-funding positions. Unset blocks prepaid-funding postings.",
    required_at=None,
    inherits=False,
    validator=_account_ref,
)

register_specs([PREPAYMENT_SPEC, PREPAID_FUNDING_SPEC])


def account_ref_matches(db: Session, tenant_id: UUID, value: object) -> list[UUID]:
    """THE resolver for an ``ar.*`` account reference (S9b F1): a canonical account id, else a code, else every account
    with that exact name (any status). It is the one order the mapper, holdings and the control-account guard all use,
    so the guard protects exactly what the mapper posts to. Zero matches is ``[]``; callers decide what several mean
    (the mapper and holdings read "not exactly one" as unset or invalid; the guard protects them all)."""
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        if str(UUID(value)) == value:
            return list(db.scalars(select(Account.id).where(Account.tenant_id == tenant_id, Account.id == UUID(value))))
    except ValueError:
        pass
    by_code = list(db.scalars(select(Account.id).where(Account.tenant_id == tenant_id, Account.code == value)))
    if by_code:
        return by_code
    return list(db.scalars(select(Account.id).where(Account.tenant_id == tenant_id, Account.name == value)))
