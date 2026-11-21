"""The billing side of the 1400 and prepayment ties (R1 and per-account positions), behind a protocol.

R1 is billing a13's public ``dotmac_billing.get_receivable_control_position_v1`` (defined in
``dotmac_billing.control_position``). ``A13Billing`` adapts it: ``default_reader()`` returns it, and the tenant is
bound with ``for_tenant`` (the holdings CLI does this from the run spec) or read from the session's
``app.current_tenant``. The adapter fails closed: any billing refusal, an unbound or mismatching tenant, or a currency
whose precision is not the ERP minor exponent refuses ``r1_unavailable`` / ``r1_tenant_unbound`` /
``r1_tenant_mismatch`` / ``r1_precision_unsupported``. Per-account positions come from the SEALED AR-opening window's
admissions (``get_current_receivable_position``) and the window manifest from ``get_opening_window``; an unsealed,
missing or different window refuses. Amounts are MINOR units (integers); ``position_fingerprint`` binds a plan to the
state it read.

R1 per currency: ``collectible_minor`` (open receivables, unknown_unverified counted: P-2), ``non_cash_minor``,
``available_credit_minor``, ``prepaid_funding_minor``. 1400 subledger = collectible - non_cash; prepayment subledger =
available_credit + prepaid_funding (both signed into GL terms by the caller: 1400 debit +, prepayment credit -).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select as sa_select
from sqlalchemy import text
from sqlalchemy.orm import Session

from erp_v2.holdings.errors import refuse
from erp_v2.migration_ledger.common import canonical_sha

MINOR_EXPONENT = 2  # the ERP holds NGN and other 2-decimal currencies; the same constant as holdings.attribution


@dataclass(frozen=True, slots=True)
class Position:
    collectible_minor: int
    non_cash_minor: int
    available_credit_minor: int
    prepaid_funding_minor: int

    def receivable(self) -> int:
        return self.collectible_minor - self.non_cash_minor

    def credit(self) -> int:
        return self.available_credit_minor + self.prepaid_funding_minor


@dataclass(frozen=True, slots=True)
class R1Snapshot:
    position_fingerprint: str
    currencies: Mapping[str, Position]


class BillingReader(Protocol):
    def r1(self, db: Session) -> R1Snapshot:
        """Billing R1, read in a REPEATABLE READ READ ONLY transaction."""

    def account_positions(self, db: Session) -> Mapping[tuple[str, str], Position]:
        """Per (legacy customer key, currency); the caller opens a READ COMMITTED read-write transaction it rolls
        back (``get_current_receivable_position`` needs read-write [E R1.1])."""

    def window_manifest_fingerprint(self, db: Session, window_id: str) -> str:
        """The billing window's bound manifest fingerprint (the a13 reader; ``manifest_fingerprint_unavailable`` until
        then). T1 compares it with the e2_0029 admission rows."""


class UnavailableBilling:
    """Billing without R1 (or no adapter): every read refuses ``r1_unavailable``."""

    def r1(self, db: Session) -> R1Snapshot:
        return refuse("r1_unavailable")

    def account_positions(self, db: Session) -> Mapping[tuple[str, str], Position]:
        return refuse("r1_unavailable")

    def window_manifest_fingerprint(self, db: Session, window_id: str) -> str:
        return refuse("manifest_fingerprint_unavailable")


class A13Billing:
    """R1 from billing a13 ``get_receivable_control_position_v1``. Read-only; the caller owns the REPEATABLE READ READ
    ONLY transaction (billing refuses any other posture, which surfaces here as ``r1_unavailable``)."""

    def __init__(self, tenant_id: UUID | None = None) -> None:
        self.tenant_id = tenant_id

    def for_tenant(self, tenant_id: UUID) -> A13Billing:
        return A13Billing(tenant_id)

    def _scope(self, db: Session):
        from dotmac_kernel.cache import TenantScope

        current = db.execute(text("SELECT current_setting('app.current_tenant', true)")).scalar_one()
        current = current or None
        if self.tenant_id is None and current is None:
            return refuse("r1_tenant_unbound", "bind a tenant (for_tenant) or set app.current_tenant")
        if self.tenant_id is None:
            try:
                return TenantScope(UUID(str(current)))
            except ValueError:
                return refuse("r1_tenant_unbound", "app.current_tenant is not a tenant id")
        if current is not None and current != str(self.tenant_id):
            return refuse("r1_tenant_mismatch", "the session tenant differs from the bound tenant")
        # Transaction-local, allowed in a READ ONLY transaction; billing requires it to equal the scope's tenant.
        db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(self.tenant_id)})
        return TenantScope(self.tenant_id)

    def r1(self, db: Session) -> R1Snapshot:
        from dotmac_billing import get_receivable_control_position_v1
        from dotmac_billing.errors import BillingError

        scope = self._scope(db)
        try:
            result = get_receivable_control_position_v1(db, scope=scope)
        except BillingError as exc:
            return refuse("r1_unavailable", str(getattr(exc, "code", "billing_refused")))
        currencies: dict[str, Position] = {}
        for row in result.currencies:
            if row.precision != MINOR_EXPONENT:
                return refuse("r1_precision_unsupported", row.currency)
            currencies[row.currency] = Position(
                collectible_minor=row.collectible_receivable_minor,
                non_cash_minor=row.available_non_cash_credit_minor,
                available_credit_minor=row.available_credit_minor,
                prepaid_funding_minor=row.prepaid_funding_minor,
            )
        # Binds the plan to exactly what billing's per-currency fingerprints cover (snapshot_at is not part of it).
        fingerprint = canonical_sha(
            {
                "contract": result.contract,
                "currencies": [
                    {"currency": c.currency, "accounts": c.accounts, "fingerprint": c.position_fingerprint}
                    for c in sorted(result.currencies, key=lambda c: c.currency)
                ],
            }
        )
        return R1Snapshot(fingerprint, currencies)

    def _window(self, db: Session, scope, window_id: str | None):
        from dotmac_billing import get_opening_window, get_opening_window_seal
        from dotmac_billing.errors import BillingError

        try:
            window = get_opening_window(db, scope=scope)
            seal = get_opening_window_seal(db, scope=scope) if window is not None else None
        except BillingError as exc:
            return refuse("billing_window_unavailable", str(getattr(exc, "code", "billing_refused")))
        if window is None:
            return refuse("billing_window_missing")
        if window_id is not None and str(window.window_id) != str(window_id):
            return refuse("billing_window_mismatch")
        if seal is None:
            return refuse("billing_window_not_sealed")
        return window

    def account_positions(self, db: Session) -> Mapping[tuple[str, str], Position]:
        """Per (legacy customer key, currency): the CURRENT position of every admitted AR-opening account, read from
        billing (READ COMMITTED, read-write: ``get_current_receivable_position`` locks the account row; the caller
        rolls back). The partitions are the ``ar_opening_admissions`` rows of the SEALED window; a row on another
        window, a duplicate key, an incomplete position or a billing refusal refuses (never a zero)."""
        from dotmac_billing import GetCurrentReceivablePositionV1, get_current_receivable_position
        from dotmac_billing.errors import BillingError
        from dotmac_kernel.money import Currency

        from erp_v2.importer.ar_opening_models import ArOpeningAdmission

        scope = self._scope(db)
        window = self._window(db, scope, None)
        rows = list(
            db.scalars(
                sa_select(ArOpeningAdmission)
                .where(ArOpeningAdmission.tenant_id == scope.tenant_id)
                .order_by(ArOpeningAdmission.source_account_key, ArOpeningAdmission.currency)
            )
        )
        seen: set[tuple[str, str]] = set()
        for row in rows:  # structural refusals first, before any billing read
            if str(row.window_id) != str(window.window_id):
                return refuse("admission_window_mismatch")
            key = (row.source_account_key, row.currency)
            if key in seen:
                return refuse("account_positions_key_duplicate")
            seen.add(key)
        out: dict[tuple[str, str], Position] = {}
        for row in rows:
            key = (row.source_account_key, row.currency)
            currency = Currency(row.currency, MINOR_EXPONENT)
            try:
                current = get_current_receivable_position(
                    db,
                    scope=scope,
                    request=GetCurrentReceivablePositionV1(scope, row.billing_account_id, currency),
                )
            except BillingError as exc:
                return refuse("account_positions_unavailable", str(getattr(exc, "code", "billing_refused")))
            position = current.position
            if position is None:
                out[key] = Position(0, 0, 0, 0)  # proven pristine history (billing's own proof), not a guess
                continue
            if position.completeness != "complete":
                return refuse("account_position_incomplete")
            out[key] = Position(
                collectible_minor=_minor(position.collectible_receivable.amount),
                non_cash_minor=_minor(position.available_non_cash_credit.amount),
                available_credit_minor=_minor(position.available_credit.amount),
                prepaid_funding_minor=_minor(position.prepaid_funding.amount),
            )
        return out

    def window_manifest_fingerprint(self, db: Session, window_id: str) -> str:
        """The sealed billing window's bound manifest fingerprint (a missing, different or unsealed window refuses)."""
        scope = self._scope(db)
        return self._window(db, scope, window_id).manifest_fingerprint


def _minor(amount) -> int:
    """An exact minor-unit integer, or refuse: a value that is not a whole number of minor units is never rounded."""
    scaled = Decimal(amount).scaleb(MINOR_EXPONENT)
    if scaled != scaled.to_integral_value():
        return refuse("r1_precision_unsupported", "amount is not a whole number of minor units")
    return int(scaled)


def default_reader() -> BillingReader:
    try:
        from dotmac_billing import get_receivable_control_position_v1  # noqa: F401
    except ImportError:  # pragma: no cover - billing a13 is a hard dependency
        return UnavailableBilling()
    return A13Billing()
