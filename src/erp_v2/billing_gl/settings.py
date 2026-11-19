"""B-15 mapper settings: typed tenant settings in the ``billing_gl`` domain, plus the two ``ar.*`` accounts.

Entered in the UI (the deploy-values rule), never hand-authored env files. ``map_digest`` is the sha over the
effective MAPPING values (accounts, timezone, closed-period policy); it is stamped on every journal as
``billing_gl:<map_digest>`` so a posting is traceable to the mapping that produced it. The on/off flag and the
window tunables are behaviour, not mapping, and are not part of it.

``ar.prepayment_liability_account`` and ``ar.prepaid_funding_account`` are registered by B-14
(``erp_v2.ledger_settings``) and are read here BY KEY STRING; an unregistered or invalid value reads as unset
(the dependent facts then BLOCK and self-heal when it is configured).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotmac_accounting.models import Account
from dotmac_kernel.settings_models import SettingDomain, SettingValueType
from dotmac_kernel.settings_resolver import SettingSpec, register_specs, resolve_value
from sqlalchemy import select
from sqlalchemy.orm import Session

DOMAIN: Final = SettingDomain("billing_gl")
# Where B-14 registers the ar.* account settings. ASSUMPTION (spec says only "by key string"): confirm at re-chain.
AR_DOMAIN: Final = SettingDomain("accounting_policy")
PREPAYMENT_KEY: Final = "ar.prepayment_liability_account"
PREPAID_FUNDING_KEY: Final = "ar.prepaid_funding_account"

NEXT_OPEN_PERIOD: Final = "next_open_period"
HOLD: Final = "hold"
OPENING_REFUND_SOURCE: Final = "opening_refund"


def _tz(value: object) -> None:
    try:
        ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Use a valid IANA timezone") from exc


def _iso_date(value: object) -> None:
    try:
        date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("Use an ISO date (YYYY-MM-DD)") from exc


def _cash_accounts(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("Use a JSON object mapping source system to account id")
    for source, account in value.items():
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Source system names must be non-empty text")
        try:
            if str(UUID(str(account))) != account:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Each cash account must be a canonical account id") from exc


SPECS: Final = [
    SettingSpec(
        domain=DOMAIN,
        key="enabled",
        value_type=SettingValueType.boolean,
        default=False,
        label="Billing to GL mapper enabled",
        description="Post billing accounting facts to the ledger. Off keeps the mapper idle (main stays deployable).",
        inherits=False,
    ),
    SettingSpec(
        domain=DOMAIN,
        key="cash_account",
        value_type=SettingValueType.json,
        default={},
        label="Cash account per billing source system",
        description=(
            "JSON object: source system -> GL account id receiving that source's cash. The key "
            f"'{OPENING_REFUND_SOURCE}' is the account for opening cash refunds. An unset source blocks its facts."
        ),
        inherits=False,
        validator=_cash_accounts,
    ),
    SettingSpec(
        domain=DOMAIN,
        key="posting_timezone",
        value_type=SettingValueType.string,
        default="Africa/Lagos",
        label="Posting timezone",
        description="IANA timezone in which a fact's business time becomes its journal posting date.",
        inherits=False,
        validator=_tz,
    ),
    SettingSpec(
        domain=DOMAIN,
        key="closed_period_policy",
        value_type=SettingValueType.string,
        default=NEXT_OPEN_PERIOD,
        label="Closed-period policy",
        description=(
            "next_open_period posts on the first open date after a closed period (the original date is on the "
            "journal); hold blocks the fact until the period is open."
        ),
        inherits=False,
        allowed={NEXT_OPEN_PERIOD, HOLD},
    ),
    SettingSpec(
        domain=DOMAIN,
        key="settle_seconds",
        value_type=SettingValueType.integer,
        default=300,
        label="Settle seconds",
        description="A window closes only this long after its end, so late-committed facts can still arrive.",
        inherits=False,
        min_value=0,
        max_value=86400,
    ),
    SettingSpec(
        domain=DOMAIN,
        key="resummarize_days",
        value_type=SettingValueType.integer,
        default=45,
        label="Re-summarise closed windows (days)",
        description="Closed windows this recent are re-checked every run; a mismatch re-pages the window.",
        inherits=False,
        min_value=0,
        max_value=3650,
    ),
    SettingSpec(
        domain=DOMAIN,
        key="history_floor",
        value_type=SettingValueType.string,
        default="2000-01-01",
        label="History floor",
        description="The first run starts paging at this date so every opening fact is dispositioned.",
        inherits=False,
        validator=_iso_date,
    ),
]

register_specs(list(SPECS))


@dataclass(frozen=True, slots=True)
class MapperSettings:
    enabled: bool
    cash_accounts: dict[str, str]
    posting_timezone: str
    closed_period_policy: str
    settle_seconds: int
    resummarize_days: int
    history_floor: date
    prepayment_account: str | None
    prepaid_funding_account: str | None

    @property
    def map_digest(self) -> str:
        body = {
            "cash_accounts": dict(sorted(self.cash_accounts.items())),
            "closed_period_policy": self.closed_period_policy,
            "posting_timezone": self.posting_timezone,
            "prepaid_funding_account": self.prepaid_funding_account,
            "prepayment_account": self.prepayment_account,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _account(db: Session, tenant_id: UUID, value: object) -> str | None:
    """An ``ar.*`` value is an account id, code or exact name; it must resolve to exactly one tenant account.

    The spec's defaults are names ("Customer prepayments"), so a non-UUID value is looked up by code, then by name.
    Zero or several matches read as unset (the dependent facts BLOCK). Postability is the bridge's check.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        if str(UUID(value)) == value:
            found = list(
                db.scalars(select(Account.id).where(Account.tenant_id == tenant_id, Account.id == UUID(value)))
            )
            return str(found[0]) if len(found) == 1 else None
    except ValueError:
        pass
    for column in (Account.code, Account.name):
        found = list(db.scalars(select(Account.id).where(Account.tenant_id == tenant_id, column == value)))
        if len(found) == 1:
            return str(found[0])
        if found:
            return None
    return None


def load(db: Session, tenant_id: UUID) -> MapperSettings:
    def read(key: str, default: object, domain: SettingDomain = DOMAIN) -> object:
        return resolve_value(db, domain, key, tenant_id=tenant_id, default=default)

    cash = read("cash_account", {})
    return MapperSettings(
        enabled=read("enabled", False) is True,
        cash_accounts={str(k): str(v) for k, v in cash.items()} if isinstance(cash, dict) else {},
        posting_timezone=str(read("posting_timezone", "Africa/Lagos")),
        closed_period_policy=str(read("closed_period_policy", NEXT_OPEN_PERIOD)),
        settle_seconds=int(read("settle_seconds", 300)),  # type: ignore[call-overload]
        resummarize_days=int(read("resummarize_days", 45)),  # type: ignore[call-overload]
        history_floor=date.fromisoformat(str(read("history_floor", "2000-01-01"))),
        prepayment_account=_account(db, tenant_id, read(PREPAYMENT_KEY, None, AR_DOMAIN)),
        prepaid_funding_account=_account(db, tenant_id, read(PREPAID_FUNDING_KEY, None, AR_DOMAIN)),
    )
