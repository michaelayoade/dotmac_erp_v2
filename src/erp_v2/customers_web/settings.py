"""Customer screen settings: the account-number series, entered in the UI (audited), never an env file."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from dotmac_kernel.settings_models import SettingDomain, SettingValueType
from dotmac_kernel.settings_resolver import SettingSpec, register_specs, resolve_value
from sqlalchemy.orm import Session

DOMAIN: Final = SettingDomain("customers_web")
KEY_SERIES_ACCOUNT_NUMBER: Final = "customers.series.account_number"

SPECS: Final = [
    SettingSpec(
        domain=DOMAIN,
        key=KEY_SERIES_ACCOUNT_NUMBER,
        value_type=SettingValueType.string,
        default=None,
        label="Customer account number series",
        description="Numbering series code used when a new customer is saved with a blank account number.",
        inherits=False,
    )
]

register_specs(list(SPECS))


def series_code(db: Session, tenant_id: UUID) -> str | None:
    value = resolve_value(db, DOMAIN, KEY_SERIES_ACCOUNT_NUMBER, tenant_id=tenant_id, default=None)
    return value.strip() if isinstance(value, str) and value.strip() else None
