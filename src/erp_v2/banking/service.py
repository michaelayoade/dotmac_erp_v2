"""Validate authenticated observations and atomically call the released banking owner.

The caller owns commit/rollback. No receiver is mounted until trusted destination
resolution, descriptor publication and scoped credential provisioning are integrated.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from dotmac_banking.contracts import BankStatementInput, StatementLineDirection, StatementLineInput
from dotmac_banking.models import BankAccount
from dotmac_banking.service import import_bank_statement
from dotmac_kernel.machine_auth import MachinePrincipal
from dotmac_kernel.money import Currency, Money
from dotmac_kernel.transactions import conflict_savepoint
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from erp_v2.banking.contracts import BankingObservationEnvelope
from erp_v2.banking.models import BankingObservationReceipt, BankingProviderBinding

WRITE_SCOPE = "integration:observations:write"
MIRROR_SCOPE = "integration:observations:mirror"


class BankingObservationError(ValueError):
    """Refuse an untrusted or inadmissible observation without financial effects."""


class BankingObservationCollision(BankingObservationError):
    """A previously accepted source identity has changed."""


@dataclass(frozen=True)
class ObservationAcceptance:
    observation_id: UUID
    statement_id: UUID
    replayed: bool


def _text(value: str) -> None:
    if not value or value.strip() != value or len(value) > 200:
        raise BankingObservationError("delivery headers must be nonempty, bounded and trimmed")


def _resolve(
    db: Session, principal: MachinePrincipal, envelope: BankingObservationEnvelope, *, write: bool
) -> tuple[BankingProviderBinding, BankAccount]:
    scope = WRITE_SCOPE if write else MIRROR_SCOPE
    if not principal.has_scope(scope):
        raise BankingObservationError("machine credential lacks required scope")
    query = select(BankingProviderBinding).where(
        BankingProviderBinding.tenant_id == principal.tenant_id,
        BankingProviderBinding.credential_id == principal.credential_id,
        BankingProviderBinding.source_application == principal.application,
        BankingProviderBinding.installation_id == envelope.source.installation_id,
        BankingProviderBinding.provider_account_id == envelope.observation.provider_account_id,
        BankingProviderBinding.scope_kind == envelope.scope.kind,
        BankingProviderBinding.scope_ref == envelope.scope.ref,
    )
    binding = db.scalar(query.with_for_update() if write else query)
    if binding is None or (write and not binding.enabled):
        raise BankingObservationError("trusted banking binding is unavailable")
    account = db.scalar(
        select(BankAccount).where(
            BankAccount.tenant_id == principal.tenant_id,
            BankAccount.id == binding.account_id,
        )
    )
    if account is None or account.status != "active":
        raise BankingObservationError("bound bank account is unavailable")
    obs = envelope.observation
    # Mono amounts are minor-unit integers. Currency exponent comes from the
    # trusted destination account; never the wire or a two-decimal assumption.
    if obs.currency != account.currency_code or not 0 <= account.minor_units <= 6:
        raise BankingObservationError("observation currency does not match bank account")
    if int(obs.amount_minor) <= 0:
        raise BankingObservationError("observation amount must be positive")
    return binding, account


def statement_command(
    envelope: BankingObservationEnvelope, binding: BankingProviderBinding, account: BankAccount
) -> BankStatementInput:
    obs = envelope.observation
    amount = Decimal(obs.amount_minor).scaleb(-account.minor_units)
    # Released module persists Numeric(20,6), regardless of currency exponent.
    if amount >= Decimal("100000000000000"):
        raise BankingObservationError("amount exceeds banking storage precision")
    identity = hashlib.sha256(
        f"{binding.installation_id}:{obs.provider_account_id}:{obs.provider_transaction_id}".encode()
    ).hexdigest()
    day = obs.business_date
    return BankStatementInput(
        account_id=account.id,
        statement_ref=f"observation:{identity}",
        period_start=day,
        period_end=day,
        opening_balance=None,
        closing_balance=None,
        source_ref=f"integrator:banking:{identity}",
        source_version="1",
        evidence_ref=f"banking-observation:{identity}",
        lines=(
            StatementLineInput(
                line_number=1,
                transaction_date=day,
                direction=StatementLineDirection(obs.direction),
                amount=Money.of(amount, Currency(account.currency_code, account.minor_units)),
                external_ref=obs.provider_transaction_id,
                description=obs.narration or "Integrator banking transaction observation",
                bank_transaction_code=obs.category,
            ),
        ),
    )


def accept_observation(
    db: Session,
    *,
    principal: MachinePrincipal,
    envelope: BankingObservationEnvelope,
    idempotency_key: str,
    correlation_id: str,
    received_at: datetime,
) -> ObservationAcceptance:
    _text(idempotency_key)
    _text(correlation_id)
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise BankingObservationError("receipt timestamp must be timezone aware")
    binding, account = _resolve(db, principal, envelope, write=True)
    fingerprint = envelope.fingerprint()
    existing = db.scalar(
        select(BankingObservationReceipt).where(
            BankingObservationReceipt.tenant_id == principal.tenant_id,
            BankingObservationReceipt.binding_id == binding.id,
            or_(
                BankingObservationReceipt.idempotency_key == idempotency_key,
                BankingObservationReceipt.provider_transaction_id == envelope.provider_event_id,
            ),
        )
    )
    if existing is not None:
        if existing.idempotency_key != idempotency_key or existing.fingerprint != fingerprint:
            raise BankingObservationCollision("banking observation identity collision")
        return ObservationAcceptance(existing.id, existing.statement_id, True)
    command = statement_command(envelope, binding, account)
    # A failing receipt flush cannot leave a statement behind if callers handle
    # a refusal. The outer request remains the only commit/rollback authority.
    with conflict_savepoint(db):
        statement = import_bank_statement(
            db,
            tenant_id=principal.tenant_id,
            command=command,
            imported_at=received_at,
            imported_by_id=principal.credential_id,
        )
        receipt = BankingObservationReceipt(
            tenant_id=principal.tenant_id,
            binding_id=binding.id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            provider_transaction_id=envelope.provider_event_id,
            statement_id=statement.id,
            correlation_id=correlation_id,
            occurred_at=envelope.observation.occurred_at,
        )
        db.add(receipt)
        db.flush()
    return ObservationAcceptance(receipt.id, statement.id, False)


def compare_observation(
    db: Session, *, principal: MachinePrincipal, envelope: BankingObservationEnvelope
) -> dict[str, object]:
    binding, account = _resolve(db, principal, envelope, write=False)
    statement_command(envelope, binding, account)
    receipt = db.scalar(
        select(BankingObservationReceipt).where(
            BankingObservationReceipt.tenant_id == principal.tenant_id,
            BankingObservationReceipt.binding_id == binding.id,
            BankingObservationReceipt.provider_transaction_id == envelope.provider_event_id,
        )
    )
    agrees = receipt is not None and receipt.fingerprint == envelope.fingerprint()
    return {
        "verdict": "match" if agrees else "missing" if receipt is None else "blocked",
        "agrees": agrees,
        "identity": envelope.provider_event_id,
        "counterpart_identity": str(receipt.id) if receipt else None,
    }
