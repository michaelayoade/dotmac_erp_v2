"""Typed tenant setting that gates bank-file generation. Default OFF, fail closed.

``treasury_policy/bank_file_generation_enabled`` is a boolean tenant setting. Only a stored,
valid, true value enables the feature: an absent row, an invalid row or any resolution error
reads as disabled. No route writes it (provisioning or the kernel settings surface does).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from dotmac_kernel.setting_scopes import SettingScope
from dotmac_kernel.settings_models import SettingDomain, SettingValueType
from dotmac_kernel.settings_resolver import (
    SettingSpec,
    StrictSettingResolutionError,
    register_specs,
    resolve_strict,
)
from sqlalchemy.orm import Session

TREASURY_POLICY_DOMAIN: Final = SettingDomain("treasury_policy")
BANK_FILE_KEY: Final = "bank_file_generation_enabled"

BANK_FILE_SPEC: Final[SettingSpec[bool]] = SettingSpec(
    domain=TREASURY_POLICY_DOMAIN,
    key=BANK_FILE_KEY,
    value_type=SettingValueType.boolean,
    default=False,
    label="Bank upload file generation",
    description=(
        "Allow treasury to generate bank upload files for approved payments. Off by default; "
        "the ERP never calls a bank, it only produces a file for manual upload."
    ),
    required_at=None,
    inherits=False,
)

register_specs([BANK_FILE_SPEC])


def bank_file_generation_enabled(db: Session, tenant_id: UUID) -> bool:
    try:
        value = resolve_strict(db, BANK_FILE_SPEC, scope=SettingScope.tenant(tenant_id))
    except StrictSettingResolutionError:
        return False
    return value is True
