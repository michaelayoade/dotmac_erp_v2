"""Customer screen commands (F-F): create, edit (rename, finance fields, notes) and status.

dotmac-customers stays the single writer of its own tables: this module calls its service commands
(``create_account``, ``transition_account``, ``set_profile``, and ``rename_account`` from a2) and never UPDATEs
``mod_customers``. The finance fields are written to the ERP-owned ``erp_customer_finance`` only.

Rules:

* each action runs in ONE savepoint (a refusal rolls everything back) and writes ONE audit event carrying field NAMES
  only: never a contact, address, tax id or other value;
* create is idempotent per hidden ``request_id`` (kernel idempotency); a blank account number is allocated from the
  ``customers.series.account_number`` series;
* the payment terms code must be a key of ``ar.payment_terms`` at save (``payment_terms_unknown``);
* rename calls customers a2 ``rename_account`` (a2 is pinned); a CLOSED customer is refused ``customer_closed`` for a
  rename, a finance-field edit and a notes edit alike (close is terminal), with nothing saved;
* no delete: legacy delete becomes CLOSED (terminal, needs an explicit confirmation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from dotmac_customers import service as customers_service
from dotmac_customers.contracts import (
    AccountStatus,
    Conflict,
    CreateCustomerAccount,
    CustomerError,
    NotFound,
    SetCustomerProfile,
)
from dotmac_customers.models import CustomerAccount, CustomerProfile
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict, execute_once, fingerprint_of
from dotmac_kernel.models import Party, Tenant
from dotmac_numbering import service as numbering
from dotmac_numbering.service import NumberingError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.ar import settings as ar_settings
from erp_v2.customers_web import models as m
from erp_v2.customers_web import settings
from erp_v2.customers_web.models import CustomerFinance

AUDIT_CREATED: Final = "erp.finance.customers.created"
AUDIT_UPDATED: Final = "erp.finance.customers.updated"
AUDIT_STATUS: Final = "erp.finance.customers.status_changed"
AUDIT_REFUSED: Final = "erp.finance.customers.refused"
AUDIT_ACTIONS: Final = (AUDIT_CREATED, AUDIT_UPDATED, AUDIT_STATUS, AUDIT_REFUSED)

MAX_DISPLAY_NAME: Final = 200
MAX_ACCOUNT_NUMBER: Final = 40
CREATE_STATUSES: Final = (AccountStatus.ACTIVE, AccountStatus.PROSPECT)  # legacy customers start active
_EMAIL: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CustomerRefused(ValueError):
    """A safe, user-facing refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, message: str, *, audit: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.audit = audit


@dataclass(frozen=True, slots=True)
class Outcome:
    account_id: UUID
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class FinanceInput:
    trading_name: str | None = None
    tax_identification_number: str | None = None
    payment_terms_code: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    billing_address: str | None = None


FINANCE_FIELDS: Final = tuple(f.name for f in fields(FinanceInput))


# --- validation -------------------------------------------------------------------------------------------------


def _text(value: str | None, label: str, limit: int, *, multiline: bool = False) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if len(value) > limit:
        raise CustomerRefused("invalid_input", f"{label} must be at most {limit} characters")
    allowed = "\n\r\t" if multiline else ""
    if any(not (ch.isprintable() or ch in allowed) for ch in value):
        raise CustomerRefused("invalid_input", f"{label} contains characters that cannot be printed")
    return value


def valid_terms(db: Session, tenant_id: UUID) -> set[str]:
    terms = ar_settings.read(db, tenant_id, ar_settings.KEY_TERMS)
    return {str(k) for k in terms} if isinstance(terms, dict) else set()


def clean_finance(raw: FinanceInput, *, terms: set[str]) -> FinanceInput:
    """Trim and validate every finance field. The tax id format is not validated (legacy)."""
    email = _text(raw.contact_email, "Contact email", m.MAX_EMAIL)
    if email is not None and not _EMAIL.match(email):
        raise CustomerRefused("invalid_email", "Enter the contact email as name@domain")
    code = _text(raw.payment_terms_code, "Payment terms", 80)
    if code is not None:
        if code not in terms:
            raise CustomerRefused("payment_terms_unknown", "The payment terms code is not one of the configured terms")
        if len(code) > m.MAX_TERMS_CODE:
            raise CustomerRefused(
                "payment_terms_code_too_long", f"A customer's terms code must be at most {m.MAX_TERMS_CODE} characters"
            )
    return FinanceInput(
        trading_name=_text(raw.trading_name, "Trading name", m.MAX_TRADING_NAME),
        tax_identification_number=_text(raw.tax_identification_number, "Tax id", m.MAX_TIN),
        payment_terms_code=code,
        contact_name=_text(raw.contact_name, "Contact name", m.MAX_CONTACT_NAME),
        contact_email=email,
        contact_phone=_text(raw.contact_phone, "Contact phone", m.MAX_PHONE),
        billing_address=_text(raw.billing_address, "Billing address", m.MAX_ADDRESS, multiline=True),
    )


def _display_name(value: str | None) -> str:
    name = _text(value, "Display name", MAX_DISPLAY_NAME)
    if name is None:
        raise CustomerRefused("display_name_required", "Enter the customer's name")
    return name


def _notes(value: str | None) -> str | None:
    return _text(value, "Notes", 4000, multiline=True)


# --- plumbing ---------------------------------------------------------------------------------------------------


def _audit(
    db: Session, *, tenant: Tenant, party: Party, entity_id: str, action: str, success: bool, details: dict[str, object]
) -> None:
    write_audit_event(
        db,
        tenant_id=tenant.id,
        actor_party_id=party.id,
        actor_type="user",
        actor_id=str(party.id),
        actor_label=getattr(party, "email", None) or None,
        source_application="dotmac_erp",
        action=action,
        entity_type="customer_account",
        entity_id=entity_id,
        is_success=success,
        details=details,
    )


def _guard(db: Session, *, tenant: Tenant, party: Party, entity_id: str, action: str, work):
    """Run ``work`` in one savepoint; audit a refusal (names only) and re-raise it."""
    if party.tenant_id != tenant.id:
        raise CustomerRefused("actor_tenant", "The acting party must belong to this tenant", audit=False)
    try:
        with db.begin_nested():
            return work()
    except CustomerRefused as exc:
        refusal = exc
    except NotFound as exc:
        refusal = CustomerRefused("not_found", str(exc), audit=False)
    except Conflict as exc:
        refusal = CustomerRefused("conflict", str(exc))
    except CustomerError as exc:  # pragma: no cover - the module's base refusal
        refusal = CustomerRefused("customer_refused", str(exc))
    except IdempotencyConflict as exc:
        refusal = CustomerRefused("idempotency_conflict", str(exc))
    except NumberingError as exc:
        refusal = CustomerRefused(getattr(exc, "code", "numbering").replace(".", "_"), str(exc))
    except IntegrityError:
        refusal = CustomerRefused("conflict", "A concurrent change prevented the action; reload and retry")
    if refusal.audit:
        _audit(
            db,
            tenant=tenant,
            party=party,
            entity_id=entity_id,
            action=AUDIT_REFUSED,
            success=False,
            details={"code": refusal.code, "attempted": action},
        )
    raise refusal


def _finance_row(db: Session, tenant_id: UUID, account_id: UUID) -> CustomerFinance | None:
    return db.scalar(
        select(CustomerFinance)
        .where(CustomerFinance.tenant_id == tenant_id, CustomerFinance.customer_account_id == account_id)
        .execution_options(populate_existing=True)
        .with_for_update()  # a concurrent edit of the same customer waits instead of losing an update
    )


def _account(db: Session, tenant_id: UUID, account_id: UUID) -> CustomerAccount:
    account = customers_service.get_account(db, scope=TenantScope(tenant_id), account_id=account_id)
    if account is None:  # pragma: no cover - required=True raises NotFound
        raise NotFound("customer account was not found in the tenant")
    return account


def _rename(db: Session, tenant_id: UUID, account_id: UUID, display_name: str) -> None:
    try:
        customers_service.rename_account(
            db, scope=TenantScope(tenant_id), account_id=account_id, display_name=display_name
        )
    except Conflict as exc:  # a2 refuses a rename of a CLOSED account
        raise CustomerRefused("customer_closed", str(exc)) from exc


# --- create -----------------------------------------------------------------------------------------------------


def _number(db: Session, tenant: Tenant, request_id: UUID, given: str | None, today: datetime) -> str:
    number = _text(given, "Account number", MAX_ACCOUNT_NUMBER)
    if number is not None:
        return number
    series = settings.series_code(db, tenant.id)
    if series is None:
        raise CustomerRefused(
            "account_number_series_unset", f"Enter an account number, or set {settings.KEY_SERIES_ACCOUNT_NUMBER}"
        )
    return numbering.allocate(
        db,
        scope=TenantScope(tenant.id),
        series_code=series,
        reference_date=today.date(),
        idempotency_key=f"erp_v2.customers.account_number:{request_id}",
    ).formatted_number


def _provided(finance: FinanceInput) -> list[str]:
    return [name for name in FINANCE_FIELDS if getattr(finance, name) is not None]


def create_customer(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    display_name: str | None,
    finance: FinanceInput,
    account_number: str | None = None,
    status: AccountStatus = AccountStatus.ACTIVE,
    notes: str | None = None,
    now: datetime | None = None,
) -> Outcome:
    now = now or datetime.now(UTC)

    def work() -> Outcome:
        name = _display_name(display_name)
        if status not in CREATE_STATUSES:
            raise CustomerRefused("invalid_input", "A new customer starts as active or prospect")
        clean = clean_finance(finance, terms=valid_terms(db, tenant.id))
        note = _notes(notes)
        given = _text(account_number, "Account number", MAX_ACCOUNT_NUMBER)

        def operation(session: Session) -> dict[str, object]:
            number = _number(session, tenant, request_id, given, now)
            try:
                account = customers_service.create_account(
                    session,
                    scope=TenantScope(tenant.id),
                    command=CreateCustomerAccount(account_number=number, display_name=name, status=status),
                )
            except Conflict as exc:
                raise CustomerRefused("account_number_taken", "That account number is already in use") from exc
            if note:
                customers_service.set_profile(
                    session, scope=TenantScope(tenant.id), command=SetCustomerProfile(account.id, notes=note)
                )
            session.add(
                CustomerFinance(
                    tenant_id=tenant.id,
                    customer_account_id=account.id,
                    updated_by=party.id,
                    created_at=now,
                    updated_at=now,
                    **{k: getattr(clean, k) for k in FINANCE_FIELDS},
                )
            )
            session.flush()
            return {"account_id": str(account.id)}

        outcome = execute_once(
            db,
            tenant_id=tenant.id,
            scope="erp_v2.customers.create",
            key=str(request_id),
            fingerprint=fingerprint_of(
                {
                    "number": given,
                    "name": name,
                    "status": status.value,
                    "notes": note,
                    **{k: getattr(clean, k) for k in FINANCE_FIELDS},
                }
            ),
            operation=operation,
        )
        account_id = UUID(str(outcome.result["account_id"]))
        if not outcome.replayed:  # a replay is a no-op, with no second audit row
            _audit(
                db,
                tenant=tenant,
                party=party,
                entity_id=str(account_id),
                action=AUDIT_CREATED,
                success=True,
                details={
                    "fields": ["display_name", *_provided(clean), *(["notes"] if note else [])],
                    "status": status.value,
                },
            )
        return Outcome(account_id, outcome.replayed)

    return _guard(db, tenant=tenant, party=party, entity_id=str(request_id), action="create", work=work)


# --- edit -------------------------------------------------------------------------------------------------------


def update_customer(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    account_id: UUID,
    display_name: str | None,
    finance: FinanceInput,
    notes: str | None = None,
    now: datetime | None = None,
) -> Outcome:
    now = now or datetime.now(UTC)

    def work() -> Outcome:
        account = _account(db, tenant.id, account_id)
        if account.status == AccountStatus.CLOSED:
            raise CustomerRefused("customer_closed", "A closed customer cannot be edited")
        name = _display_name(display_name)
        clean = clean_finance(finance, terms=valid_terms(db, tenant.id))
        note = _notes(notes)
        changed: list[str] = []
        if name != account.display_name:
            _rename(db, tenant.id, account_id, name)
            changed.append("display_name")
        profile = db.scalar(
            select(CustomerProfile).where(
                CustomerProfile.tenant_id == tenant.id, CustomerProfile.account_id == account_id
            )
        )
        if (profile.notes if profile else None) != note:
            customers_service.set_profile(
                db,
                scope=TenantScope(tenant.id),
                command=SetCustomerProfile(account_id, segment=profile.segment if profile else None, notes=note),
            )
            changed.append("notes")
        row = _finance_row(db, tenant.id, account_id)
        if row is None:
            row = CustomerFinance(
                tenant_id=tenant.id,
                customer_account_id=account_id,
                updated_by=party.id,
                created_at=now,
                updated_at=now,
                **{k: getattr(clean, k) for k in FINANCE_FIELDS},
            )
            db.add(row)
            changed.extend(_provided(clean))
        else:
            edits = [k for k in FINANCE_FIELDS if getattr(row, k) != getattr(clean, k)]
            for k in edits:
                setattr(row, k, getattr(clean, k))
            if edits:
                row.updated_at, row.updated_by = now, party.id
            changed.extend(edits)
        db.flush()
        if changed:
            _audit(
                db,
                tenant=tenant,
                party=party,
                entity_id=str(account_id),
                action=AUDIT_UPDATED,
                success=True,
                details={"fields": changed},
            )
        return Outcome(account_id)

    return _guard(db, tenant=tenant, party=party, entity_id=str(account_id), action="update", work=work)


# --- status -----------------------------------------------------------------------------------------------------


def change_status(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    account_id: UUID,
    target: AccountStatus,
    confirm_close: bool = False,
) -> Outcome:
    def work() -> Outcome:
        if target == AccountStatus.CLOSED and not confirm_close:
            raise CustomerRefused("close_unconfirmed", "Closing a customer is permanent; confirm to proceed")
        before = _account(db, tenant.id, account_id).status
        if before == target:
            return Outcome(account_id)
        try:
            customers_service.transition_account(db, scope=TenantScope(tenant.id), account_id=account_id, target=target)
        except Conflict as exc:
            code = "customer_closed" if before == AccountStatus.CLOSED else "status_transition_refused"
            raise CustomerRefused(code, str(exc)) from exc
        _audit(
            db,
            tenant=tenant,
            party=party,
            entity_id=str(account_id),
            action=AUDIT_STATUS,
            success=True,
            details={"from": before.value, "to": target.value},
        )
        return Outcome(account_id)

    return _guard(db, tenant=tenant, party=party, entity_id=str(account_id), action="status", work=work)
