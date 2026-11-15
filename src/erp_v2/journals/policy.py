"""Tenant functional currency: one typed, tenant-scoped setting, never a literal.

The value comes from legacy ``organization.functional_currency_code`` at import
(``importer.minting.mint_functional_currency_settings``). Manual journals and every
ERP posting path read it here; no route or service names a currency code. It is
read-only in the UI (no route writes it) and immutable once any journal beyond DRAFT
exists for the tenant: ``set_functional_currency`` then refuses any different value.
A tenant with no value refuses to prepare or post (fail closed; there is no default).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from dotmac_accounting.contracts import JournalStatus
from dotmac_accounting.models import JournalEntry
from dotmac_kernel.setting_scopes import SettingScope
from dotmac_kernel.settings_models import SettingDomain, SettingValueType
from dotmac_kernel.settings_resolver import (
    SettingChangeContext,
    SettingSpec,
    StrictSettingResolutionError,
    register_specs,
    resolve_strict,
    upsert_by_key,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

ACCOUNTING_POLICY_DOMAIN: Final = SettingDomain("accounting_policy")
FUNCTIONAL_CURRENCY_KEY: Final = "functional_currency"


class FunctionalCurrencyError(ValueError):
    """Fail-closed refusal; ``code`` is stable and machine-readable."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def normalize_currency(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Use a three-letter ISO currency code")
    code = value.strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise ValueError("Use a three-letter ISO currency code")
    return code


def _validator(value: object) -> None:
    if normalize_currency(value) != value:
        raise ValueError("Use an upper-case three-letter ISO currency code")


FUNCTIONAL_CURRENCY_SPEC: Final[SettingSpec[str | None]] = SettingSpec(
    domain=ACCOUNTING_POLICY_DOMAIN,
    key=FUNCTIONAL_CURRENCY_KEY,
    value_type=SettingValueType.string,
    default=None,
    label="Functional currency",
    description=(
        "Tenant functional (book) currency, imported from the legacy organisation. "
        "Read-only once any journal is posted."
    ),
    required_at="tenant",
    inherits=False,
    validator=_validator,
)

register_specs([FUNCTIONAL_CURRENCY_SPEC])


def functional_currency(db: Session, tenant_id: UUID) -> str:
    """The tenant's functional currency, or refuse. Never defaults."""
    try:
        value = resolve_strict(db, FUNCTIONAL_CURRENCY_SPEC, scope=SettingScope.tenant(tenant_id))
    except StrictSettingResolutionError as exc:
        raise FunctionalCurrencyError(
            "functional_currency_unset", "the tenant has no valid functional currency setting"
        ) from exc
    if not isinstance(value, str):
        raise FunctionalCurrencyError("functional_currency_unset", "the tenant has no functional currency setting")
    return value


def has_posted_journal(db: Session, tenant_id: UUID) -> bool:
    return (
        db.scalar(
            select(JournalEntry.id)
            .where(JournalEntry.tenant_id == tenant_id, JournalEntry.status != JournalStatus.DRAFT)
            .limit(1)
        )
        is not None
    )


def set_functional_currency(
    db: Session, *, tenant_id: UUID, code: str, changed_by_party_id: UUID | None = None, reason: str
) -> str:
    """Set the value, only while no journal beyond DRAFT exists; identical value is a no-op.

    Called by provisioning code, never by a route. Once any journal is posted the
    value is locked: a different value refuses ``functional_currency_locked``.
    """
    try:
        normalized = normalize_currency(code)
    except ValueError as exc:
        raise FunctionalCurrencyError("functional_currency_invalid", str(exc)) from exc
    try:
        current: str | None = functional_currency(db, tenant_id)
    except FunctionalCurrencyError:
        current = None
    if current == normalized:
        return current
    if has_posted_journal(db, tenant_id):
        raise FunctionalCurrencyError(
            "functional_currency_locked", "a journal is already posted; the functional currency is immutable"
        )
    upsert_by_key(
        db,
        ACCOUNTING_POLICY_DOMAIN,
        FUNCTIONAL_CURRENCY_KEY,
        normalized,
        tenant_id=tenant_id,
        changed_by=SettingChangeContext(actor_party_id=changed_by_party_id, reason=reason),
    )
    return normalized
