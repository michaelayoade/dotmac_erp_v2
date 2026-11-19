"""B-16 settings: typed tenant settings in the ``ar_producer`` domain. Entered in the UI (audited), never env files.

Keys carry the spec's names (``ar.producer.enabled`` ...). Code constants, NOT settings (a setting must never
widen a control): the confirmation literal, the obligation source kind and the funding lane (``constants``).
``ar.default_revenue_account`` pre-fills the form only; it is never a mapper fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from dotmac_kernel.setting_scopes import SettingScope
from dotmac_kernel.settings_models import SettingDomain, SettingValueType
from dotmac_kernel.settings_resolver import (
    SettingSpec,
    StrictSettingResolutionError,
    register_specs,
    resolve_strict,
    resolve_value,
)
from sqlalchemy.orm import Session

DOMAIN: Final = SettingDomain("ar_producer")

KEY_ENABLED: Final = "ar.producer.enabled"
KEY_SERIES_INVOICE: Final = "ar.series.invoice"
KEY_SERIES_CREDIT_NOTE: Final = "ar.series.credit_note"
KEY_SERIES_RECEIPT: Final = "ar.series.receipt"
KEY_TERMS: Final = "ar.payment_terms"
KEY_DEFAULT_TERMS: Final = "ar.default_payment_terms_code"
KEY_DEFAULT_REVENUE: Final = "ar.default_revenue_account"
KEY_SELLER: Final = "ar.seller_snapshot"
KEY_PAYMENT_INSTRUCTIONS: Final = "ar.payment_instructions"
KEY_PROFILE_CODE: Final = "ar.document_profile_code"
KEY_PROFILE_VERSION: Final = "ar.document_profile_version"
KEY_AUTO_APPLY: Final = "ar.auto_apply_credit_on_issue"

# Required before F10 (the setup half of the gate check): no default can stand in for these.
REQUIRED_KEYS: Final = (
    KEY_SERIES_INVOICE,
    KEY_SERIES_CREDIT_NOTE,
    KEY_SERIES_RECEIPT,
    KEY_TERMS,
    KEY_DEFAULT_TERMS,
    KEY_SELLER,
    KEY_PAYMENT_INSTRUCTIONS,
)


def _terms(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("Use a JSON object mapping a terms code to a number of days")
    for code, days in value.items():
        if not isinstance(code, str) or not code.strip() or len(code) > 80:
            raise ValueError("Terms codes must be non-empty text of at most 80 characters")
        if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 3650:
            raise ValueError("Terms days must be a whole number from 0 to 3650")


def _seller(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("Use a JSON object with legal_name, address_line and country_code")
    for key in ("legal_name", "address_line", "country_code"):
        if key in value and not isinstance(value[key], str):
            raise ValueError(f"{key} must be text")


def _instructions(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("Use a JSON object with method_code and the bank details to print")
    if any(not isinstance(v, str) for v in value.values()):
        raise ValueError("Every payment instruction value must be text")


def _text(key: str, label: str, description: str, default: str | None, *, validator=None) -> SettingSpec:
    return SettingSpec(
        domain=DOMAIN,
        key=key,
        value_type=SettingValueType.string,
        default=default,
        label=label,
        description=description,
        inherits=False,
        validator=validator,
    )


SPECS: Final = [
    SettingSpec(
        domain=DOMAIN,
        key=KEY_ENABLED,
        value_type=SettingValueType.boolean,
        default=False,
        label="AR producer enabled",
        description="Allow issuing invoices and credit notes and recording receipts. Off: every action refuses.",
        inherits=False,
    ),
    _text(KEY_SERIES_INVOICE, "Invoice number series", "Numbering series code for customer invoices.", None),
    _text(KEY_SERIES_CREDIT_NOTE, "Credit note number series", "Numbering series code for credit notes.", None),
    _text(KEY_SERIES_RECEIPT, "Receipt number series", "Numbering series code for customer receipts.", None),
    SettingSpec(
        domain=DOMAIN,
        key=KEY_TERMS,
        value_type=SettingValueType.json,
        default={},
        label="Payment terms",
        description="JSON object: terms code -> days until due (from the legacy payment terms).",
        inherits=False,
        validator=_terms,
    ),
    _text(KEY_DEFAULT_TERMS, "Default payment terms code", "Terms code pre-selected on a new invoice.", None),
    _text(
        KEY_DEFAULT_REVENUE,
        "Default revenue account",
        "Account id pre-filled on invoice lines. Pre-fill only: a line always carries its own revenue account.",
        None,
    ),
    SettingSpec(
        domain=DOMAIN,
        key=KEY_SELLER,
        value_type=SettingValueType.json,
        default={},
        label="Seller identity on documents",
        description="JSON object: legal_name, address_line, country_code (default NG), optional trading_name.",
        inherits=False,
        validator=_seller,
    ),
    SettingSpec(
        domain=DOMAIN,
        key=KEY_PAYMENT_INSTRUCTIONS,
        value_type=SettingValueType.json,
        default={},
        label="Payment instructions on documents",
        description="JSON object: method_code and optional bank_name, account_name, account_reference, narrative.",
        inherits=False,
        validator=_instructions,
    ),
    _text(
        KEY_PROFILE_CODE,
        "Document profile code",
        "Billing document profile for invoices and credit notes.",
        "erp_v2.invoice",
    ),
    _text(KEY_PROFILE_VERSION, "Document profile version", "Billing document profile version.", "1"),
    SettingSpec(
        domain=DOMAIN,
        key=KEY_AUTO_APPLY,
        value_type=SettingValueType.boolean,
        default=True,
        label="Apply customer credit when an invoice is issued",
        description="Legacy behaviour: unallocated cleared receipts are applied FIFO to a newly issued invoice.",
        inherits=False,
    ),
]

register_specs(list(SPECS))

_SPEC_BY_KEY: Final = {spec.key: spec for spec in SPECS}


def producer_enabled(db: Session, tenant_id: UUID) -> bool:
    """Fail closed: only a stored, valid ``true`` enables the producer."""
    try:
        value = resolve_strict(db, _SPEC_BY_KEY[KEY_ENABLED], scope=SettingScope.tenant(tenant_id))
    except StrictSettingResolutionError:
        return False
    return value is True


def read(db: Session, tenant_id: UUID, key: str) -> object:
    spec = _SPEC_BY_KEY[key]
    return resolve_value(db, DOMAIN, key, tenant_id=tenant_id, default=spec.default)


@dataclass(frozen=True, slots=True)
class ArSettings:
    series_invoice: str | None
    series_credit_note: str | None
    series_receipt: str | None
    terms: dict[str, int]
    default_terms_code: str | None
    default_revenue_account: str | None
    seller: dict[str, str]
    payment_instructions: dict[str, str]
    profile_code: str
    profile_version: str
    auto_apply: bool

    def missing(self) -> list[str]:
        """Required keys that are unset or unusable (the ``ar_setup_incomplete:<key>`` reasons)."""
        gaps = []
        if not self.series_invoice:
            gaps.append(KEY_SERIES_INVOICE)
        if not self.series_credit_note:
            gaps.append(KEY_SERIES_CREDIT_NOTE)
        if not self.series_receipt:
            gaps.append(KEY_SERIES_RECEIPT)
        if not self.terms:
            gaps.append(KEY_TERMS)
        if not self.default_terms_code or self.default_terms_code not in self.terms:
            gaps.append(KEY_DEFAULT_TERMS)
        if not (self.seller.get("legal_name") and self.seller.get("address_line")):
            gaps.append(KEY_SELLER)
        if not self.payment_instructions.get("method_code"):
            gaps.append(KEY_PAYMENT_INSTRUCTIONS)
        return gaps


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value: object) -> dict[str, str]:
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def load(db: Session, tenant_id: UUID) -> ArSettings:
    terms = read(db, tenant_id, KEY_TERMS)
    return ArSettings(
        series_invoice=_str(read(db, tenant_id, KEY_SERIES_INVOICE)),
        series_credit_note=_str(read(db, tenant_id, KEY_SERIES_CREDIT_NOTE)),
        series_receipt=_str(read(db, tenant_id, KEY_SERIES_RECEIPT)),
        terms={str(k): int(v) for k, v in terms.items()} if isinstance(terms, dict) else {},
        default_terms_code=_str(read(db, tenant_id, KEY_DEFAULT_TERMS)),
        default_revenue_account=_str(read(db, tenant_id, KEY_DEFAULT_REVENUE)),
        seller=_mapping(read(db, tenant_id, KEY_SELLER)),
        payment_instructions=_mapping(read(db, tenant_id, KEY_PAYMENT_INSTRUCTIONS)),
        profile_code=_str(read(db, tenant_id, KEY_PROFILE_CODE)) or "erp_v2.invoice",
        profile_version=_str(read(db, tenant_id, KEY_PROFILE_VERSION)) or "1",
        auto_apply=read(db, tenant_id, KEY_AUTO_APPLY) is not False,
    )
