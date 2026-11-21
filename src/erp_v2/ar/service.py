"""AR producer (B-16): invoices, credit notes, receipts, allocations on dotmac-billing commands.

Billing owns the financial truth. This module is the ERP composition: it validates the ERP-owned inputs (revenue
accounts, tax, periods, the receiving bank), runs the billing commands in ONE savepoint per action (any refusal rolls
back every billing and ERP row of that action) and records the ERP-owned structural rows the B-15 mapper needs
(``erp_ar_line_accounts``, ``erp_ar_receipts``). It never posts to the ledger.

Rules carried from the spec:

* every billing key derives from the draft, document or request id, so a double POST or a retry returns the original;
* the funding lane is always ``available_credit``; withholding / non-cash / refund commands are NEVER called (R9,
  asserted by a source-grep test); only this package calls billing write commands (second grep test);
* D-9: a draft is stamped with the tenant policy at creation; issue / post refuse ``same_person`` when stamped ON.
  Void / apply / deallocate / reverse read the live policy and then require a different person from the original issuer;
* the actor is always the authenticated party, never request data;
* functional currency only (``fx_unsupported``); negative / discount lines refused; no standalone credit note.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Final
from uuid import UUID

from dotmac_accounting.contracts import AccountKind, PeriodStatus
from dotmac_accounting.models import Account, FiscalPeriod
from dotmac_banking.models import BankAccount
from dotmac_billing import models as bm
from dotmac_billing import service as billing
from dotmac_billing.commands import (
    AllocationCommand,
    ApplyAvailableCreditCommand,
    CreateDraftDocument,
    CreateMultiLineDraftDocument,
    DeallocationCommand,
    DraftDocumentLine,
    IssueCreditNote,
    IssueDocument,
    ReversePostingGroupCommand,
)
from dotmac_billing.contracts import (
    AcceptRatedObligationV1,
    AcceptSettlementV1,
    AppliedTaxSnapshotV1,
    PartyDocumentSnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
    PresentationAssetReferenceV1,
    ServicePeriodEvidenceV1,
    ServicePeriodStatus,
    SettlementFundingLane,
)
from dotmac_billing.earmarks import allocation_remaining
from dotmac_billing.errors import BillingError
from dotmac_billing.opening_commands import ReverseCashCreditDrawV1
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict, execute_once, fingerprint_of
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.money import Money
from dotmac_kernel.money import currency as currency_of
from dotmac_kernel.settings_models import DomainSettingHistory
from dotmac_numbering.service import NumberingError
from dotmac_tax import service as tax_service
from dotmac_tax.contracts import TaxFact
from dotmac_tax.models import TaxCode, TaxJurisdiction, TaxRule
from dotmac_tax.service import TaxConflict, TaxNotFound, TaxRuleViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2 import ledger_roles
from erp_v2.ar import constants, numbering, queries, settings, terms
from erp_v2.ar.models import (
    KIND_CREDIT_NOTE,
    KIND_INVOICE,
    KIND_RECEIPT,
    STATUS_DELETED,
    STATUS_DRAFT,
    STATUS_ISSUED,
    ArDraft,
    ArDraftLine,
    ArLineAccount,
    ArReceipt,
)
from erp_v2.disbursement import bank_reference as bank_ref
from erp_v2.governance import policy as governance
from erp_v2.journals import policy as journal_policy
from erp_v2.posting import rules as posting_rules

AUDIT_DRAFT_SAVED: Final = "erp.finance.ar.draft_saved"
AUDIT_DRAFT_DISCARDED: Final = "erp.finance.ar.draft_discarded"
AUDIT_INVOICE_ISSUED: Final = "erp.finance.ar.invoice_issued"
AUDIT_CREDIT_NOTE_ISSUED: Final = "erp.finance.ar.credit_note_issued"
AUDIT_RECEIPT_POSTED: Final = "erp.finance.ar.receipt_posted"
AUDIT_RECEIPT_REVERSED: Final = "erp.finance.ar.receipt_reversed"
AUDIT_CREDIT_APPLIED: Final = "erp.finance.ar.credit_applied"
AUDIT_DEALLOCATED: Final = "erp.finance.ar.deallocated"
AUDIT_DOCUMENT_VOIDED: Final = "erp.finance.ar.document_voided"
AUDIT_REFUSED: Final = "erp.finance.ar.refused"
AUDIT_ACTIONS: Final = (
    AUDIT_DRAFT_SAVED,
    AUDIT_DRAFT_DISCARDED,
    AUDIT_INVOICE_ISSUED,
    AUDIT_CREDIT_NOTE_ISSUED,
    AUDIT_RECEIPT_POSTED,
    AUDIT_RECEIPT_REVERSED,
    AUDIT_CREDIT_APPLIED,
    AUDIT_DEALLOCATED,
    AUDIT_DOCUMENT_VOIDED,
    AUDIT_REFUSED,
)

REVERSAL_REASONS: Final = ("void", "bounced")
_SIX: Final = Decimal("0.000001")


class ArRefused(ValueError):
    """A safe, user-facing refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, message: str, *, audit: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.audit = audit


@dataclass(frozen=True, slots=True)
class Outcome:
    id: UUID
    replayed: bool
    document_id: UUID | None = None
    settlement_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class LineInput:
    description: str
    quantity: Decimal
    unit_amount: Decimal
    revenue_account_id: UUID | None = None
    tax_code_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AllocationInput:
    target_document_id: UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class _Priced:
    """One validated line: exact pre-tax, tax and the AppliedTaxSnapshot (when taxed)."""

    n: int
    description: str
    quantity: Decimal
    unit_amount: Decimal
    pre_tax: Decimal
    tax: Decimal
    revenue_account_id: UUID
    tax_code_id: UUID | None
    treatment_code: str | None
    snapshot: AppliedTaxSnapshotV1 | None


# --- keys (every one derives from a draft / document / request id; a test asserts the length) -----------------


def key_draft(request_id: UUID) -> str:
    return f"erp_v2.ar.draft:{request_id}"


def key_invoice(draft_id: UUID) -> str:
    return f"erp_v2.ar.inv:{draft_id}"


def key_credit_note(draft_id: UUID) -> str:
    return f"erp_v2.ar.cn:{draft_id}"


def key_line(draft_id: UUID, n: int) -> str:
    return f"{draft_id}:{n}"


def key_autoapply(document_id: UUID) -> str:
    return f"erp_v2.ar.autoapply:{document_id}"


def key_apply(request_id: UUID) -> str:
    return f"erp_v2.ar.apply:{request_id}"


def key_alloc(draft_id: UUID, n: int) -> str:
    return f"erp_v2.ar.alloc:{draft_id}:{n}"


def key_dealloc(allocation_id: UUID, request_id: UUID) -> str:
    return f"erp_v2.ar.dealloc:{allocation_id}:{request_id}"


def key_draw_reverse(group_id: UUID) -> str:
    return f"erp_v2.ar.drawrev:{group_id}"


def key_reverse_draw(settlement_id: UUID, group_id: UUID) -> str:
    return f"erp_v2.ar.rdraw:{settlement_id}:{group_id}"


def key_reverse(settlement_id: UUID) -> str:
    return f"erp_v2.ar.reverse:{settlement_id}"


def key_reverse_dealloc(settlement_id: UUID, allocation_id: UUID) -> str:
    return f"erp_v2.ar.rdealloc:{settlement_id}:{allocation_id}"


def key_void(document_id: UUID) -> str:
    return f"erp_v2.ar.void:{document_id}"


def key_receipt_number(draft_id: UUID) -> str:
    return f"erp_v2.ar.receipt:{draft_id}"


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
        entity_type="ar_document",
        entity_id=entity_id,
        is_success=success,
        details=details,
    )


def _scope(tenant: Tenant) -> TenantScope:
    return TenantScope(tenant.id)


def _functional(db: Session, tenant_id: UUID) -> str:
    try:
        return journal_policy.functional_currency(db, tenant_id)
    except journal_policy.FunctionalCurrencyError as exc:
        raise ArRefused("functional_currency_unset", "The tenant has no functional currency setting") from exc


def _money(amount: Decimal, code: str) -> Money:
    try:
        return Money.of(amount, currency_of(code))
    except Exception as exc:
        raise ArRefused("amount_not_exact", "Amounts must be exact at the currency's minor units") from exc


def _lagos_today(now: datetime) -> date:
    return now.astimezone(terms.LAGOS).date()


def _guard(db: Session, *, tenant: Tenant, party: Party, entity_id: str, action: str, work):
    """Refuse when disabled (writes nothing); else run ``work`` in one savepoint, audit and re-raise a refusal."""
    if party.tenant_id != tenant.id:
        raise ArRefused("actor_tenant", "The acting party must belong to this tenant", audit=False)
    if not settings.producer_enabled(db, tenant.id):
        raise ArRefused("ar_producer_disabled", "Receivables entry is not enabled for this tenant", audit=False)
    try:
        with db.begin_nested():
            return work()
    except ArRefused as exc:
        refusal = exc
    except BillingError as exc:
        code = exc.code.removeprefix("billing.")
        refusal = ArRefused(code, exc.message)
    except IdempotencyConflict as exc:
        refusal = ArRefused("idempotency_conflict", str(exc))
    except (TaxConflict, TaxNotFound, TaxRuleViolation) as exc:
        refusal = ArRefused("tax_refused", str(exc))
    except NumberingError as exc:
        refusal = ArRefused(exc.code.replace(".", "_"), str(exc))
    except IntegrityError:
        refusal = ArRefused("conflict", "A concurrent change prevented the action; reload and retry")
    except posting_rules.PostingRefused as exc:  # pragma: no cover - this package never posts
        refusal = ArRefused(f"posting_{exc.code}", "The ledger refused the posting")
    if refusal.audit and refusal.code != "not_found":
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


def _locked_draft(db: Session, tenant_id: UUID, draft_id: UUID) -> ArDraft:
    row = db.scalar(
        select(ArDraft)
        .where(ArDraft.tenant_id == tenant_id, ArDraft.id == draft_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ArRefused("not_found", "The draft was not found")
    return row


def _dual_control_live(db: Session, tenant_id: UUID) -> bool:
    return governance.dual_control(db, tenant_id)


def _require_other_person(db: Session, tenant: Tenant, party: Party, original_actor: UUID | None) -> None:
    """Void / apply / deallocate / reverse: with D-9 ON (live policy) the actor must differ from the original issuer."""
    if original_actor is not None and original_actor == party.id and _dual_control_live(db, tenant.id):
        raise ArRefused("same_person", "Another person must do this: you issued the original document")


# --- validation -------------------------------------------------------------------------------------------------


def _revenue_account(db: Session, tenant_id: UUID, account_id: UUID | None) -> Account:
    if account_id is None:
        raise ArRefused("revenue_account_required", "Every line needs a revenue account")
    account = db.scalar(select(Account).where(Account.tenant_id == tenant_id, Account.id == account_id))
    if account is None:
        raise ArRefused("revenue_account_unknown", "A line's revenue account was not found")
    if not account.is_active or not account.posting_allowed or account.kind != AccountKind.POSTING:
        raise ArRefused("revenue_account_not_postable", f"Account {account.code} is not open for posting")
    if account.id in ledger_roles.control_account_ids(db, tenant_id) or account.code == constants.AR_CONTROL_CODE:
        raise ArRefused("revenue_account_is_control", f"Account {account.code} is a control account")
    if str(account.id) in ledger_roles.tax_role_account_ids(db, tenant_id):
        raise ArRefused("revenue_account_is_tax", f"Account {account.code} holds VAT and cannot be a revenue account")
    if account.id in queries.holding_account_ids(db, tenant_id):
        raise ArRefused("revenue_account_is_holding", f"Account {account.code} is a migration holding account")
    return account


def _require_postable_date(db: Session, tenant_id: UUID, day: date, now: datetime) -> None:
    if day > _lagos_today(now):
        raise ArRefused("date_future", "The date is in the future")
    periods = list(
        db.scalars(
            select(FiscalPeriod).where(
                FiscalPeriod.tenant_id == tenant_id, FiscalPeriod.start_date <= day, FiscalPeriod.end_date >= day
            )
        )
    )
    if len(periods) != 1:
        raise ArRefused("period_missing", "The date is not in exactly one fiscal period")
    period = periods[0]
    if period.status != PeriodStatus.OPEN:
        raise ArRefused("period_closed", f"The fiscal period is {period.status.value.lower()}")
    if period.is_adjustment:
        raise ArRefused("period_adjustment_only", "The period accepts adjustment entries only")


def _exact_product(quantity: Decimal, unit_amount: Decimal, code: str) -> Decimal:
    minor = currency_of(code).minor_units
    with localcontext() as ctx:
        ctx.prec = 60
        product = quantity * unit_amount
        exact = product.quantize(Decimal(1).scaleb(-minor))
    if exact != product:
        raise ArRefused("line_amount_not_exact", "Quantity times unit amount must be exact at the currency's precision")
    return exact


def _line_tax(
    db: Session, *, tenant_id: UUID, tax_code_id: UUID, pre_tax: Decimal, code: str, on: date, source_ref: str
) -> tuple[Decimal, AppliedTaxSnapshotV1, str]:
    """Tax for one line from the dotmac-tax rule of the selected code (billing never computes rates)."""
    tax_code = db.scalar(select(TaxCode).where(TaxCode.tenant_id == tenant_id, TaxCode.id == tax_code_id))
    if tax_code is None or tax_code.status != "active":
        raise ArRefused("tax_code_unknown", "The selected tax code was not found or is not active")
    rule = db.scalar(
        select(TaxRule)
        .where(
            TaxRule.tenant_id == tenant_id,
            TaxRule.tax_code_id == tax_code_id,
            TaxRule.is_published.is_(True),
            TaxRule.effective_from <= on,
            (TaxRule.effective_to.is_(None)) | (TaxRule.effective_to >= on),
        )
        .order_by(TaxRule.priority, TaxRule.version.desc())
        .limit(1)
    )
    if rule is None:
        raise ArRefused("tax_rule_missing", "The tax code has no published rule for the document date")
    if rule.calculation_method != "percentage" or rule.inclusive or rule.rate is None:
        raise ArRefused("tax_rule_unsupported", "Only exclusive percentage tax rules are supported on invoices")
    jurisdiction = db.scalar(
        select(TaxJurisdiction).where(
            TaxJurisdiction.tenant_id == tenant_id, TaxJurisdiction.id == tax_code.jurisdiction_id
        )
    )
    if jurisdiction is None:
        raise ArRefused("tax_code_unknown", "The tax code's jurisdiction was not found")
    fact = TaxFact(
        jurisdiction_id=jurisdiction.id,
        occurred_on=on,
        fact_kind=rule.fact_kind,
        recognition_basis_code=rule.recognition_basis_code,
        transaction_side=rule.transaction_side,
        base_amount=_money(pre_tax, code),
        source_ref=source_ref,
        source_version="1",
        evidence_ref=source_ref,
        party_category=rule.party_category,
        supply_category=rule.supply_category,
        place_code=rule.place_code,
    )
    preview = tax_service.preview_tax_policy(db, tenant_id=tenant_id, fact=fact, determined_at=datetime.now(UTC))
    components = [c for c in preview.components if c.tax_code_id == tax_code_id]
    if len(components) != 1 or len(components[0].lines) != 1 or components[0].inclusive:
        raise ArRefused("tax_rule_unsupported", "The tax code did not resolve to one exclusive percentage component")
    component = components[0]
    money = component.tax_amount
    snapshot = AppliedTaxSnapshotV1(
        treatment_code=component.treatment_code,
        jurisdiction_code=jurisdiction.code,
        policy_id=str(component.rule_id),
        policy_version=str(component.rule_version),
        rate=component.lines[0].rate,
        taxable_basis=component.base_amount,
        tax_amount=money,
    )
    return money.amount, snapshot, component.treatment_code


def _price_lines(
    db: Session,
    *,
    tenant_id: UUID,
    lines: Sequence[LineInput | ArDraftLine],
    code: str,
    on: date,
    source_prefix: str,
) -> list[_Priced]:
    if not lines:
        raise ArRefused("no_lines", "Add at least one line")
    if len(lines) > constants.MAX_LINES:
        raise ArRefused("too_many_lines", f"A document carries at most {constants.MAX_LINES} lines")
    priced: list[_Priced] = []
    for n, line in enumerate(lines, 1):
        description = (line.description or "").strip()
        quantity, unit_amount = line.quantity, line.unit_amount
        if not description:
            raise ArRefused("description_required", f"Line {n} needs a description")
        if quantity is None or unit_amount is None:
            raise ArRefused("invalid_amount", f"Line {n} needs a quantity and a unit amount")
        if (
            not quantity.is_finite()
            or not unit_amount.is_finite()
            or quantity != quantity.quantize(_SIX)
            or quantity >= Decimal("1e14")
        ):
            raise ArRefused("invalid_amount", f"Line {n}: quantity must have at most six decimals")
        if quantity < 0 or unit_amount < 0:
            raise ArRefused("negative_line_unsupported", f"Line {n}: negative and discount lines are not supported")
        pre_tax = _exact_product(quantity, unit_amount, code)
        if pre_tax <= 0:
            raise ArRefused("invalid_amount", f"Line {n}: the line amount must be greater than zero")
        account = _revenue_account(db, tenant_id, line.revenue_account_id)
        tax, snapshot, treatment = Decimal(0), None, None
        if line.tax_code_id is not None:
            tax, snapshot, treatment = _line_tax(
                db,
                tenant_id=tenant_id,
                tax_code_id=line.tax_code_id,
                pre_tax=pre_tax,
                code=code,
                on=on,
                source_ref=f"{source_prefix}:{n}",
            )
        priced.append(
            _Priced(
                n, description, quantity, unit_amount, pre_tax, tax, account.id, line.tax_code_id, treatment, snapshot
            )
        )
    return priced


# --- drafts -----------------------------------------------------------------------------------------------------


def resolve_terms_code(
    db: Session, tenant_id: UUID, kind: str, customer_account_id: UUID | None, explicit: str | None
) -> str | None:
    """Terms code stored on an invoice draft at create/update: the chosen one, else the customer's default (the seam
    below). Issue reads only the stored code, so a later change of the customer's terms never moves a draft."""
    if kind != KIND_INVOICE:
        return explicit
    return explicit or default_terms_for_customer(db, tenant_id, customer_account_id, settings.load(db, tenant_id))


def _stored_terms_code(draft: ArDraft, cfg: settings.ArSettings) -> str:
    """Issue reads only the draft's own code (resolved at create/update); an empty or unknown one refuses."""
    code = draft.payment_terms_code
    if not code:
        raise ArRefused("payment_terms_missing", "The draft has no payment terms; edit it and select one")
    if code not in cfg.terms:
        raise ArRefused("payment_terms_unknown", "The invoice's payment terms are not configured")
    return code


def default_terms_for_customer(
    db: Session, tenant_id: UUID, customer_account_id: UUID | None, cfg: settings.ArSettings
) -> str | None:
    """Seam: pre-selected terms for a customer. Today the tenant default; the customer screen swaps in the customer's
    ``erp_customer_finance`` terms here (one hunk), nowhere else."""
    return cfg.default_terms_code


def _customer_document_snapshot(db: Session, tenant_id: UUID, customer: Any) -> PartyDocumentSnapshotV1:
    """Seam: the customer snapshot on a document. Today the name and an unrecorded address; the customer screen adds
    its address / TIN fields here (one hunk)."""
    return _customer_snapshot(customer.display_name)


def _customer_snapshot(display_name: str) -> PartyDocumentSnapshotV1:
    return PartyDocumentSnapshotV1(
        legal_name=display_name,
        address=PostalAddressSnapshotV1(line_one=constants.ADDRESS_NOT_RECORDED, country_code="NG"),
    )


def _seller_snapshot(cfg: settings.ArSettings) -> PartyDocumentSnapshotV1:
    return PartyDocumentSnapshotV1(
        legal_name=cfg.seller["legal_name"],
        trading_name=cfg.seller.get("trading_name") or None,
        address=PostalAddressSnapshotV1(
            line_one=cfg.seller["address_line"], country_code=cfg.seller.get("country_code") or "NG"
        ),
    )


def _instructions(cfg: settings.ArSettings) -> PaymentInstructionsSnapshotV1:
    allowed = ("method_code", "bank_name", "account_name", "account_reference", "routing_code", "narrative")
    return PaymentInstructionsSnapshotV1(**{k: v for k, v in cfg.payment_instructions.items() if k in allowed})


def _terms_version(db: Session, tenant_id: UUID) -> str:
    """The settings audit version id of the payment terms table (the due-date basis provenance)."""
    row = db.scalar(
        select(DomainSettingHistory.id)
        .where(
            DomainSettingHistory.tenant_id == tenant_id,
            DomainSettingHistory.domain == settings.DOMAIN,
            DomainSettingHistory.key == settings.KEY_TERMS,
        )
        .order_by(DomainSettingHistory.changed_at.desc())
        .limit(1)
    )
    return str(row) if row is not None else "default"


def _draft_payload(
    kind: str,
    customer_account_id: UUID,
    document_date: date,
    lines: Sequence[LineInput],
    original_document_id: UUID | None,
    bank_account_id: UUID | None,
    bank_reference: str | None,
    amount: Decimal | None,
    reason: str | None,
    allocations: Sequence[AllocationInput],
    payment_terms_code: str | None = None,
    due_date: date | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "customer_account_id": str(customer_account_id),
        "document_date": document_date.isoformat(),
        "original_document_id": str(original_document_id) if original_document_id else None,
        "bank_account_id": str(bank_account_id) if bank_account_id else None,
        "bank_reference": bank_reference,
        "amount": str(amount) if amount is not None else None,
        "reason": reason,
        "payment_terms_code": payment_terms_code,
        "due_date": due_date.isoformat() if due_date else None,
        "lines": [
            [
                ln.description,
                str(ln.quantity),
                str(ln.unit_amount),
                str(ln.revenue_account_id),
                str(ln.tax_code_id),
            ]
            for ln in lines
        ],
        "allocations": [[str(a.target_document_id), str(a.amount)] for a in allocations],
    }


def _check_draft_shape(
    db: Session,
    *,
    tenant: Tenant,
    kind: str,
    customer_account_id: UUID,
    document_date: date,
    lines: Sequence[LineInput],
    allocations: Sequence[AllocationInput],
    original_document_id: UUID | None,
    amount: Decimal | None,
    now: datetime,
    payment_terms_code: str | None = None,
    due_date: date | None = None,
) -> str:
    """Early validation at draft time (the issue re-validates against the then-current ledger). Returns the currency."""
    code = _functional(db, tenant.id)
    if queries.customer(db, tenant.id, customer_account_id) is None:
        raise ArRefused("customer_unknown", "Select one of the tenant's customers")
    if document_date > _lagos_today(now):
        raise ArRefused("date_future", "The date is in the future")
    if kind == KIND_INVOICE:
        if payment_terms_code is not None and payment_terms_code not in settings.load(db, tenant.id).terms:
            raise ArRefused("payment_terms_unknown", "Select one of the configured payment terms")
        if due_date is not None and due_date < document_date:
            raise ArRefused("due_date_before_invoice", "The due date cannot be before the invoice date")
        _price_lines(db, tenant_id=tenant.id, lines=lines, code=code, on=document_date, source_prefix="draft")
    elif kind == KIND_CREDIT_NOTE:
        if original_document_id is None:
            raise ArRefused("credit_note_requires_invoice", "A credit note is issued against one named invoice")
        _require_single_credit_line(lines)
        _price_lines(db, tenant_id=tenant.id, lines=lines, code=code, on=document_date, source_prefix="draft")
    elif kind == KIND_RECEIPT:
        _check_receipt_shape(amount, allocations)
    else:
        raise ArRefused("invalid_kind", "Unknown document kind")
    return code


def _require_single_credit_line(lines: Sequence[object]) -> None:
    """Billing writes ONE line per credit note and the B-15 mapper compares per-line rows until it compares sums
    (#77): a multi-line credit note would BLOCK for good (the rows are append-only), so refuse it for now."""
    if len(lines) > 1:
        raise ArRefused(
            "credit_note_single_line_only", "A credit note carries one line for now; issue one note per line"
        )


def _check_receipt_shape(amount: Decimal | None, allocations: Sequence[AllocationInput]) -> None:
    if amount is None or not amount.is_finite() or amount <= 0:
        raise ArRefused("invalid_amount", "Enter the amount received")
    seen: set[UUID] = set()
    total = Decimal(0)
    for allocation in allocations:
        if allocation.amount is None or not allocation.amount.is_finite() or allocation.amount <= 0:
            raise ArRefused("invalid_amount", "Every allocation needs a positive amount")
        if allocation.target_document_id in seen:
            raise ArRefused("duplicate_target", "An invoice appears once per receipt")
        seen.add(allocation.target_document_id)
        total += allocation.amount
    if total > amount:
        raise ArRefused("allocation_exceeds_receipt", "The allocations exceed the amount received")


def _replace_lines(
    db: Session, tenant_id: UUID, draft: ArDraft, lines: Sequence[LineInput], allocations: Sequence[AllocationInput]
) -> None:
    for old in queries.draft_lines(db, tenant_id, draft.id):
        db.delete(old)
    db.flush()
    n = 0
    for line in lines:
        n += 1
        db.add(
            ArDraftLine(
                tenant_id=tenant_id,
                draft_id=draft.id,
                n=n,
                description=line.description,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                revenue_account_id=line.revenue_account_id,
                tax_code_id=line.tax_code_id,
            )
        )
    for allocation in allocations:
        n += 1
        db.add(
            ArDraftLine(
                tenant_id=tenant_id,
                draft_id=draft.id,
                n=n,
                target_document_id=allocation.target_document_id,
                amount=allocation.amount,
            )
        )
    db.flush()


def _create_draft(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    kind: str,
    customer_account_id: UUID,
    document_date: date,
    lines: Sequence[LineInput],
    allocations: Sequence[AllocationInput],
    original_document_id: UUID | None,
    bank_account_id: UUID | None,
    bank_reference: str | None,
    amount: Decimal | None,
    reason: str | None,
    payment_terms_code: str | None,
    due_date: date | None,
    now: datetime,
) -> Outcome:
    code = _check_draft_shape(
        db,
        tenant=tenant,
        kind=kind,
        customer_account_id=customer_account_id,
        document_date=document_date,
        lines=lines,
        allocations=allocations,
        original_document_id=original_document_id,
        amount=amount,
        payment_terms_code=payment_terms_code,
        due_date=due_date,
        now=now,
    )

    def operation(session: Session) -> dict[str, object]:
        draft = ArDraft(
            tenant_id=tenant.id,
            kind=kind,
            customer_account_id=customer_account_id,
            currency=code,
            document_date=document_date,
            original_document_id=original_document_id,
            bank_account_id=bank_account_id,
            bank_reference=bank_reference,
            amount=amount,
            reason=reason,
            payment_terms_code=resolve_terms_code(session, tenant.id, kind, customer_account_id, payment_terms_code),
            due_date=due_date,
            status=STATUS_DRAFT,
            prepared_by=party.id,
            dual_control=governance.dual_control(session, tenant.id),
            request_id=request_id,
        )
        session.add(draft)
        session.flush()
        _replace_lines(session, tenant.id, draft, lines, allocations)
        return {"draft_id": str(draft.id)}

    outcome = execute_once(
        db,
        tenant_id=tenant.id,
        scope="erp_v2.ar.draft",
        key=key_draft(request_id),
        fingerprint=fingerprint_of(
            _draft_payload(
                kind,
                customer_account_id,
                document_date,
                lines,
                original_document_id,
                bank_account_id,
                bank_reference,
                amount,
                reason,
                allocations,
                payment_terms_code,
                due_date,
            )
        ),
        operation=operation,
    )
    draft_id = UUID(str(outcome.result["draft_id"]))
    if not outcome.replayed:
        _audit(
            db,
            tenant=tenant,
            party=party,
            entity_id=str(draft_id),
            action=AUDIT_DRAFT_SAVED,
            success=True,
            details={"kind": kind, "customer_account_id": str(customer_account_id)},
        )
    return Outcome(draft_id, outcome.replayed)


def create_draft(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    kind: str,
    customer_account_id: UUID,
    document_date: date,
    lines: Sequence[LineInput] = (),
    allocations: Sequence[AllocationInput] = (),
    original_document_id: UUID | None = None,
    bank_account_id: UUID | None = None,
    bank_reference: str | None = None,
    amount: Decimal | None = None,
    reason: str | None = None,
    payment_terms_code: str | None = None,
    due_date: date | None = None,
    now: datetime | None = None,
) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(request_id),
        action="create_draft",
        work=lambda: _create_draft(
            db,
            tenant=tenant,
            party=party,
            request_id=request_id,
            kind=kind,
            customer_account_id=customer_account_id,
            document_date=document_date,
            lines=lines,
            allocations=allocations,
            original_document_id=original_document_id,
            bank_account_id=bank_account_id,
            bank_reference=bank_reference,
            amount=amount,
            reason=reason,
            payment_terms_code=payment_terms_code,
            due_date=due_date,
            now=moment,
        ),
    )


def _update_draft(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    draft_id: UUID,
    customer_account_id: UUID,
    document_date: date,
    lines: Sequence[LineInput],
    allocations: Sequence[AllocationInput],
    original_document_id: UUID | None,
    bank_account_id: UUID | None,
    bank_reference: str | None,
    amount: Decimal | None,
    reason: str | None,
    payment_terms_code: str | None,
    due_date: date | None,
    now: datetime,
) -> Outcome:
    draft = _locked_draft(db, tenant.id, draft_id)
    if draft.status != STATUS_DRAFT:
        raise ArRefused("not_draft", "Only a draft can be edited")
    _require_preparer_when_dual(draft, party)
    _check_draft_shape(
        db,
        tenant=tenant,
        kind=draft.kind,
        customer_account_id=customer_account_id,
        document_date=document_date,
        lines=lines,
        allocations=allocations,
        original_document_id=original_document_id,
        amount=amount,
        payment_terms_code=payment_terms_code,
        due_date=due_date,
        now=now,
    )
    draft.payment_terms_code = resolve_terms_code(db, tenant.id, draft.kind, customer_account_id, payment_terms_code)
    draft.due_date = due_date
    draft.customer_account_id = customer_account_id
    draft.document_date = document_date
    draft.original_document_id = original_document_id
    draft.bank_account_id = bank_account_id
    draft.bank_reference = bank_reference
    draft.amount = amount
    draft.reason = reason
    draft.updated_at = now
    db.flush()
    _replace_lines(db, tenant.id, draft, lines, allocations)
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(draft.id),
        action=AUDIT_DRAFT_SAVED,
        success=True,
        details={"kind": draft.kind, "edited": True},
    )
    return Outcome(draft.id, False)


def update_draft(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    draft_id: UUID,
    customer_account_id: UUID,
    document_date: date,
    lines: Sequence[LineInput] = (),
    allocations: Sequence[AllocationInput] = (),
    original_document_id: UUID | None = None,
    bank_account_id: UUID | None = None,
    bank_reference: str | None = None,
    amount: Decimal | None = None,
    reason: str | None = None,
    payment_terms_code: str | None = None,
    due_date: date | None = None,
    now: datetime | None = None,
) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(draft_id),
        action="update_draft",
        work=lambda: _update_draft(
            db,
            tenant=tenant,
            party=party,
            draft_id=draft_id,
            customer_account_id=customer_account_id,
            document_date=document_date,
            lines=lines,
            allocations=allocations,
            original_document_id=original_document_id,
            bank_account_id=bank_account_id,
            bank_reference=bank_reference,
            amount=amount,
            reason=reason,
            payment_terms_code=payment_terms_code,
            due_date=due_date,
            now=moment,
        ),
    )


def _discard_draft(db: Session, *, tenant: Tenant, party: Party, draft_id: UUID, now: datetime) -> Outcome:
    draft = _locked_draft(db, tenant.id, draft_id)
    if draft.status == STATUS_DELETED:
        return Outcome(draft.id, True)
    if draft.status != STATUS_DRAFT:
        raise ArRefused("not_draft", "Only a draft can be deleted")
    _require_preparer_when_dual(draft, party)
    draft.status = STATUS_DELETED
    draft.updated_at = now
    db.flush()
    _audit(
        db, tenant=tenant, party=party, entity_id=str(draft.id), action=AUDIT_DRAFT_DISCARDED, success=True, details={}
    )
    return Outcome(draft.id, False)


def discard_draft(db: Session, *, tenant: Tenant, party: Party, draft_id: UUID, now: datetime | None = None) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(draft_id),
        action="discard_draft",
        work=lambda: _discard_draft(db, tenant=tenant, party=party, draft_id=draft_id, now=moment),
    )


# --- issue ------------------------------------------------------------------------------------------------------


def _require_setup(cfg: settings.ArSettings, *keys: str) -> None:
    gaps = [k for k in cfg.missing() if k in keys]
    if gaps:
        raise ArRefused(f"ar_setup_incomplete:{gaps[0]}", f"Receivables setup is incomplete: {gaps[0]}")


def _require_preparer_when_dual(draft: ArDraft, party: Party) -> None:
    """D-9 ON: a draft is locked to its preparer, so a second person cannot rewrite it and then issue it."""
    if draft.dual_control and draft.prepared_by != party.id:
        raise ArRefused("not_preparer", "Only the person who prepared this draft can change it")


def _require_dual_stamp(draft: ArDraft, party: Party) -> None:
    if draft.dual_control and draft.prepared_by == party.id:
        raise ArRefused("same_person", "You prepared this draft; another person must issue it")


def _insert_line_accounts(
    db: Session, tenant_id: UUID, document_id: UUID, priced: Sequence[_Priced], obligation_ids: Sequence[UUID | None]
) -> None:
    for item, obligation_id in zip(priced, obligation_ids, strict=True):
        db.add(
            ArLineAccount(
                tenant_id=tenant_id,
                billing_document_id=document_id,
                line_number=item.n,
                obligation_id=obligation_id,
                revenue_account_id=item.revenue_account_id,
                pre_tax=item.pre_tax,
                tax=item.tax,
                treatment_code=item.treatment_code,
            )
        )
    db.flush()


def _issue_invoice(db: Session, *, tenant: Tenant, party: Party, draft: ArDraft, now: datetime) -> Outcome:
    scope = _scope(tenant)
    cfg = settings.load(db, tenant.id)
    _require_setup(cfg, settings.KEY_SERIES_INVOICE, settings.KEY_DEFAULT_TERMS, settings.KEY_SELLER)
    _require_setup(cfg, settings.KEY_PAYMENT_INSTRUCTIONS)
    code = _functional(db, tenant.id)
    if draft.currency != code:
        raise ArRefused("fx_unsupported", "Only functional-currency documents are supported")
    _require_postable_date(db, tenant.id, draft.document_date, now)
    customer = queries.customer(db, tenant.id, draft.customer_account_id)
    if customer is None:
        raise ArRefused("customer_unknown", "The customer was not found")
    priced = _price_lines(
        db,
        tenant_id=tenant.id,
        lines=queries.draft_lines(db, tenant.id, draft.id),
        code=code,
        on=draft.document_date,
        source_prefix=str(draft.id),
    )
    money = currency_of(code)
    account = billing.create_billing_account(
        db,
        scope=scope,
        external_account_ref=str(draft.customer_account_id),
        currency=code,
        minor_units=money.minor_units,
    )
    at = terms.midnight(draft.document_date)
    obligations = []
    for item in priced:
        obligations.append(
            billing.accept_rated_obligation(
                db,
                scope=scope,
                command=AcceptRatedObligationV1(
                    scope=scope,
                    billing_account_id=account.id,
                    contract_line_ref=constants.CONTRACT_LINE_REF,
                    contract_version=constants.CONTRACT_VERSION,
                    charge_component=constants.CHARGE_COMPONENT,
                    source_system=constants.SOURCE_SYSTEM,
                    source_kind=constants.SOURCE_KIND,
                    source_fact_id=key_line(draft.id, item.n),
                    source_fact_version="1",
                    subject_ref=str(draft.customer_account_id),
                    service_ref=None,
                    service_period=ServicePeriodEvidenceV1(status=ServicePeriodStatus.NOT_APPLICABLE),
                    collection_timing="arrears",
                    pre_tax_amount=_money(item.pre_tax, code),
                    tax_amount=_money(item.tax, code),
                    total_amount=_money(item.pre_tax + item.tax, code),
                    rated_at=at,
                    price_version_id=constants.PRICE_VERSION,
                    tax_snapshots=(item.snapshot,) if item.snapshot is not None else (),
                ),
                accepted_source_kinds=constants.ACCEPTED_SOURCE_KINDS,
            )
        )
    terms_code = _stored_terms_code(draft, cfg)
    if draft.due_date is not None:
        if draft.due_date < draft.document_date:
            raise ArRefused("due_date_before_invoice", "The due date cannot be before the invoice date")
        basis = terms.manual_basis(
            invoice_date=draft.document_date,
            terms_code=terms_code,
            terms_version=_terms_version(db, tenant.id),
            evidence_ref=f"erp_v2.ar.draft:{draft.id}",
            actor_ref=str(draft.prepared_by),  # the preparer typed the manual due date
        )
        due_at = terms.midnight(draft.due_date)
    else:
        basis = terms.net_days_basis(
            invoice_date=draft.document_date,
            terms_code=terms_code,
            terms_version=_terms_version(db, tenant.id),
            evidence_ref=f"erp_v2.ar.draft:{draft.id}",
        )
        due_at = terms.due_at(draft.document_date, cfg.terms[terms_code])
    header = {
        "seller_snapshot": _seller_snapshot(cfg),
        "customer_snapshot": _customer_document_snapshot(db, tenant.id, customer),
        "payment_instructions": _instructions(cfg),
        "brand_asset": PresentationAssetReferenceV1.none(),
        "locale": constants.LOCALE,
        "timezone": constants.TIMEZONE,
        "document_profile_code": cfg.profile_code,
        "document_profile_version": cfg.profile_version,
        "due_date_basis": basis,
    }
    if len(priced) == 1:
        item = priced[0]
        document = billing.create_draft_document(
            db,
            scope=scope,
            command=CreateDraftDocument(
                obligation_id=obligations[0].id,
                description=item.description,
                quantity=item.quantity,
                unit_code="unit",
                **header,
            ),
        )
    else:
        document = billing.create_multi_line_draft_document(
            db,
            scope=scope,
            command=CreateMultiLineDraftDocument(
                billing_account_id=account.id,
                idempotency_key=key_invoice(draft.id),
                lines=tuple(
                    DraftDocumentLine(
                        obligation_id=obligation.id,
                        description=item.description,
                        quantity=item.quantity,
                        unit_code="unit",
                        unit_amount=_money(item.unit_amount, code),
                    )
                    for item, obligation in zip(priced, obligations, strict=True)
                ),
                **header,
            ),
        )
    issued = billing.issue_document(
        db,
        scope=scope,
        command=IssueDocument(
            document_id=document.id,
            series_code=cfg.series_invoice or "",
            reference_date=draft.document_date,
            due_at=due_at,
            due_date_basis=basis,
            actor_ref=str(party.id),
            correlation_id=key_invoice(draft.id),
        ),
        numbering=numbering.Adapter(),
    )
    lines = queries.billing_lines(db, tenant.id, issued.id)
    by_obligation = {line.obligation_id: line.line_number for line in lines}
    order = [by_obligation.get(obligation.id) for obligation in obligations]
    if order != [item.n for item in priced]:
        raise ArRefused("line_order_mismatch", "Billing returned the document lines in a different order")
    _insert_line_accounts(db, tenant.id, issued.id, priced, [o.id for o in obligations])
    applied = Decimal(0)
    if cfg.auto_apply:
        applied = _auto_apply(db, tenant=tenant, scope=scope, document=issued, code=code, at=at)
    draft.status = STATUS_ISSUED
    draft.issued_by = party.id
    draft.billing_document_id = issued.id
    draft.updated_at = now
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(issued.id),
        action=AUDIT_INVOICE_ISSUED,
        success=True,
        details={
            "draft_id": str(draft.id),
            "number": issued.document_number,
            "grand_total": str(issued.grand_total),
            "auto_applied": str(applied),
        },
    )
    return Outcome(draft.id, False, document_id=issued.id)


def _auto_apply(db: Session, *, tenant: Tenant, scope: TenantScope, document: Any, code: str, at: datetime) -> Decimal:
    """Legacy: unallocated cleared receipts of the customer are applied FIFO to a newly issued invoice."""
    position = billing.rebuild_receivable_position(
        db,
        scope=scope,
        billing_account_id=document.billing_account_id,
        currency=code,
        minor_units=currency_of(code).minor_units,
    )
    available = position.available_credit
    if available <= 0:
        return Decimal(0)
    amount = min(available, document.grand_total)
    billing.apply_available_credit(
        db,
        scope=scope,
        command=ApplyAvailableCreditCommand(
            billing_account_id=document.billing_account_id,
            document_id=document.id,
            amount=_money(amount, code),
            occurred_at=at,
            source_ref=key_autoapply(document.id),
        ),
    )
    return amount


def _issue_credit_note(db: Session, *, tenant: Tenant, party: Party, draft: ArDraft, now: datetime) -> Outcome:
    scope = _scope(tenant)
    cfg = settings.load(db, tenant.id)
    _require_setup(cfg, settings.KEY_SERIES_CREDIT_NOTE)
    code = _functional(db, tenant.id)
    if draft.currency != code:
        raise ArRefused("fx_unsupported", "Only functional-currency documents are supported")
    if draft.original_document_id is None:
        raise ArRefused("credit_note_requires_invoice", "A credit note is issued against one named invoice")
    _require_postable_date(db, tenant.id, draft.document_date, now)
    original = queries.billing_document(db, tenant.id, draft.original_document_id)
    if original is None or original.document_kind != "invoice" or original.issued_at is None:
        raise ArRefused("credit_target_invalid", "A credit note requires an issued invoice")
    account = queries.billing_account(db, tenant.id, original.billing_account_id)
    if account is None or account.external_account_ref != str(draft.customer_account_id):
        raise ArRefused("credit_customer_mismatch", "The invoice belongs to a different customer")
    if queries.is_voided(db, tenant.id, original.id):
        raise ArRefused("credit_target_invalid", "The invoice has been voided")
    priced = _price_lines(
        db,
        tenant_id=tenant.id,
        lines=queries.draft_lines(db, tenant.id, draft.id),
        code=code,
        on=draft.document_date,
        source_prefix=f"cn:{draft.id}",
    )
    _require_single_credit_line(priced)
    on_original = queries.original_treatments(db, tenant.id, original.id)
    for item in priced:
        if item.treatment_code is not None and item.treatment_code not in on_original:
            # The mapper needs a VAT position of this treatment on the original: it would BLOCK after billing issued.
            raise ArRefused(
                "credit_tax_treatment_mismatch",
                f"Line {item.n}: the VAT treatment is not one the original invoice carries",
            )
    pre_tax = sum((p.pre_tax for p in priced), Decimal(0))
    tax = sum((p.tax for p in priced), Decimal(0))
    note = billing.issue_credit_note(
        db,
        scope=scope,
        command=IssueCreditNote(
            original_document_id=original.id,
            pre_tax_amount=_money(pre_tax, code),
            tax_amount=_money(tax, code),
            total_amount=_money(pre_tax + tax, code),
            reason=(draft.reason or "").strip() or "credit note",
            series_code=cfg.series_credit_note or "",
            reference_date=draft.document_date,
            actor_ref=str(party.id),
            correlation_id=key_credit_note(draft.id),
            occurred_at=terms.midnight(draft.document_date),
            document_profile_code=cfg.profile_code,
            document_profile_version=cfg.profile_version,
            tax_snapshots=tuple(p.snapshot for p in priced if p.snapshot is not None),
        ),
        numbering=numbering.Adapter(),
    )
    _insert_line_accounts(db, tenant.id, note.id, priced, [None] * len(priced))
    draft.status = STATUS_ISSUED
    draft.issued_by = party.id
    draft.billing_document_id = note.id
    draft.updated_at = now
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(note.id),
        action=AUDIT_CREDIT_NOTE_ISSUED,
        success=True,
        details={
            "draft_id": str(draft.id),
            "original_document_id": str(original.id),
            "number": note.document_number,
            "total": str(note.grand_total),
        },
    )
    return Outcome(draft.id, False, document_id=note.id)


def _issue(db: Session, *, tenant: Tenant, party: Party, draft_id: UUID, now: datetime) -> Outcome:
    draft = _locked_draft(db, tenant.id, draft_id)
    if draft.status == STATUS_ISSUED and draft.kind != KIND_RECEIPT:
        return Outcome(draft.id, True, document_id=draft.billing_document_id)  # a double POST is a no-op
    if draft.status != STATUS_DRAFT:
        raise ArRefused("not_draft", "Only a draft can be issued")
    if draft.kind == KIND_RECEIPT:
        raise ArRefused("use_post_receipt", "Receipts are posted, not issued")
    _require_dual_stamp(draft, party)
    if draft.kind == KIND_INVOICE:
        return _issue_invoice(db, tenant=tenant, party=party, draft=draft, now=now)
    return _issue_credit_note(db, tenant=tenant, party=party, draft=draft, now=now)


def issue_draft(db: Session, *, tenant: Tenant, party: Party, draft_id: UUID, now: datetime | None = None) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(draft_id),
        action="issue",
        work=lambda: _issue(db, tenant=tenant, party=party, draft_id=draft_id, now=moment),
    )


# --- receipts ---------------------------------------------------------------------------------------------------


def _cash_account(db: Session, *, tenant_id: UUID, bank_account_id: UUID, code: str) -> UUID:
    """The receiving bank's cash GL account, snapshotted at posting (never typed). Locks the bank row FOR SHARE.

    FOR SHARE: a cash GL re-point (erp_v2.cash_gl) holds the bank account row FOR UPDATE while it checks for
    payments in flight, so a receipt cannot be recorded against the old reference while a re-point commits.
    """
    bank = db.scalar(
        select(BankAccount)
        .where(BankAccount.tenant_id == tenant_id, BankAccount.id == bank_account_id)
        .with_for_update(read=True)
    )
    if bank is None:
        raise ArRefused("bank_account_unknown", "Select one of the tenant's bank accounts")
    if bank.status != "active":
        raise ArRefused("bank_account_inactive", "The selected bank account is not active")
    if bank.currency_code != code:
        raise ArRefused("bank_account_currency", "The bank account currency differs from the receipt currency")
    try:
        _, value = posting_rules.classify_ref(posting_rules.RefPolicy(), bank.cash_account_ref)
    except posting_rules.PostingRefused as exc:
        raise ArRefused(
            "cash_account_invalid", "The bank account's cash ledger account is not a canonical account id"
        ) from exc
    gl = db.scalar(select(Account).where(Account.tenant_id == tenant_id, Account.id == UUID(value)))
    if gl is None or not gl.is_active or not gl.posting_allowed or gl.kind != AccountKind.POSTING:
        raise ArRefused("cash_account_not_postable", "The bank account's cash ledger account is not open for posting")
    if gl.id in ledger_roles.control_account_ids(db, tenant_id) or gl.code == constants.AR_CONTROL_CODE:
        raise ArRefused("cash_account_is_control", "The bank account's cash ledger account is a control account")
    return gl.id


def _post_receipt(db: Session, *, tenant: Tenant, party: Party, draft_id: UUID, now: datetime) -> Outcome:
    scope = _scope(tenant)
    draft = _locked_draft(db, tenant.id, draft_id)
    if draft.kind != KIND_RECEIPT:
        raise ArRefused("not_receipt", "This draft is not a receipt")
    if draft.status == STATUS_ISSUED:
        return Outcome(draft.id, True, settlement_id=draft.billing_settlement_id)  # a replay is a no-op
    if draft.status != STATUS_DRAFT:
        raise ArRefused("not_draft", "Only a draft receipt can be posted")
    _require_dual_stamp(draft, party)
    cfg = settings.load(db, tenant.id)
    _require_setup(cfg, settings.KEY_SERIES_RECEIPT)
    code = _functional(db, tenant.id)
    if draft.currency != code:
        raise ArRefused("fx_unsupported", "Only functional-currency documents are supported")
    if draft.bank_account_id is None or draft.amount is None:
        raise ArRefused("receipt_incomplete", "A receipt needs a bank account and an amount")
    _require_postable_date(db, tenant.id, draft.document_date, now)
    cash_account_id = _cash_account(db, tenant_id=tenant.id, bank_account_id=draft.bank_account_id, code=code)
    try:
        reference = bank_ref.normalize(draft.bank_reference)
    except bank_ref.BankReferenceInvalid as exc:
        raise ArRefused(
            "bank_reference_invalid", "Enter the bank's evidence reference (3 to 255 printable characters)"
        ) from exc
    reused = db.scalar(
        select(ArReceipt.id).where(
            ArReceipt.tenant_id == tenant.id,
            ArReceipt.bank_account_id == draft.bank_account_id,
            ArReceipt.bank_reference == reference,
        )
    )
    if reused is not None:
        raise ArRefused("bank_reference_reused", "This bank reference was already used on this bank account")
    customer = queries.customer(db, tenant.id, draft.customer_account_id)
    if customer is None:
        raise ArRefused("customer_unknown", "The customer was not found")
    allocations = [row for row in queries.draft_lines(db, tenant.id, draft.id) if row.target_document_id is not None]
    _check_receipt_shape(
        draft.amount,
        [AllocationInput(row.target_document_id, row.amount) for row in allocations],  # type: ignore[arg-type]
    )
    account = billing.create_billing_account(
        db,
        scope=scope,
        external_account_ref=str(draft.customer_account_id),
        currency=code,
        minor_units=currency_of(code).minor_units,
    )
    at = terms.midnight(draft.document_date)
    settlement = billing.accept_settlement(
        db,
        scope=scope,
        command=AcceptSettlementV1(
            scope=scope,
            billing_account_id=account.id,
            source_system=constants.SETTLEMENT_SOURCE_SYSTEM,
            source_settlement_key=str(draft.id),
            source_version=constants.SETTLEMENT_SOURCE_VERSION,
            amount=_money(draft.amount, code),
            occurred_at=at,
            observed_at=now,
            confirmation_evidence=constants.CONFIRMATION_EVIDENCE,
            funding_lane=SettlementFundingLane.AVAILABLE_CREDIT,
        ),
        accepted_confirmation_evidence=constants.ACCEPTED_CONFIRMATION_EVIDENCE,
    )
    for row in allocations:
        billing.allocate_settlement(
            db,
            scope=scope,
            command=AllocationCommand(
                settlement_id=settlement.id,
                document_id=row.target_document_id,  # type: ignore[arg-type]
                amount=_money(row.amount, code),  # type: ignore[arg-type]
                occurred_at=at,
                source_ref=key_alloc(draft.id, row.n),
            ),
        )
    number = numbering.allocate_number(
        db,
        scope=scope,
        series_code=cfg.series_receipt or "",
        reference_date=draft.document_date,
        idempotency_key=key_receipt_number(draft.id),
        document_reference=key_receipt_number(draft.id),
    )
    db.add(
        ArReceipt(
            tenant_id=tenant.id,
            billing_settlement_id=settlement.id,
            draft_id=draft.id,
            bank_account_id=draft.bank_account_id,
            cash_account_id=cash_account_id,
            bank_reference=reference,
            receipt_number=number,
            received_on=draft.document_date,
            recorded_by=party.id,
        )
    )
    draft.status = STATUS_ISSUED
    draft.issued_by = party.id
    draft.billing_settlement_id = settlement.id
    draft.updated_at = now
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(settlement.id),
        action=AUDIT_RECEIPT_POSTED,
        success=True,
        details={
            "draft_id": str(draft.id),
            "receipt_number": number,
            "amount": str(draft.amount),
            "bank_reference": reference,
            "allocations": len(allocations),
        },
    )
    return Outcome(draft.id, False, settlement_id=settlement.id)


def post_receipt(db: Session, *, tenant: Tenant, party: Party, draft_id: UUID, now: datetime | None = None) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(draft_id),
        action="post_receipt",
        work=lambda: _post_receipt(db, tenant=tenant, party=party, draft_id=draft_id, now=moment),
    )


# --- apply credit, deallocate, reverse, void --------------------------------------------------------------------


def _document_issuer(db: Session, tenant_id: UUID, document_id: UUID) -> UUID | None:
    draft = queries.draft_for_document(db, tenant_id, document_id)
    return draft.issued_by if draft is not None else None


def _action_time(db: Session, tenant_id: UUID, occurred_on: date, now: datetime) -> datetime:
    _require_postable_date(db, tenant_id, occurred_on, now)  # the mapper cannot post into a closed period
    return terms.midnight(occurred_on)


def _apply_credit(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    billing_account_id: UUID,
    document_id: UUID,
    amount: Decimal,
    occurred_on: date,
    request_id: UUID,
    now: datetime,
) -> Outcome:
    scope = _scope(tenant)
    code = _functional(db, tenant.id)
    if queries.posting_group_for_source(db, tenant.id, key_apply(request_id)) is not None:
        return Outcome(document_id, True, document_id=document_id)  # a replay is a no-op, with no second audit row
    at = _action_time(db, tenant.id, occurred_on, now)
    document = queries.billing_document(db, tenant.id, document_id)
    if document is None or document.billing_account_id != billing_account_id:
        raise ArRefused("not_found", "The invoice was not found on this account")
    _require_other_person(db, tenant, party, _document_issuer(db, tenant.id, document_id))
    billing.apply_available_credit(
        db,
        scope=scope,
        command=ApplyAvailableCreditCommand(
            billing_account_id=billing_account_id,
            document_id=document_id,
            amount=_money(amount, code),
            occurred_at=at,
            source_ref=key_apply(request_id),
        ),
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(document_id),
        action=AUDIT_CREDIT_APPLIED,
        success=True,
        details={"amount": str(amount), "request_id": str(request_id)},
    )
    return Outcome(document_id, False, document_id=document_id)


def apply_credit(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    billing_account_id: UUID,
    document_id: UUID,
    amount: Decimal,
    occurred_on: date,
    request_id: UUID,
    now: datetime | None = None,
) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(document_id),
        action="apply_credit",
        work=lambda: _apply_credit(
            db,
            tenant=tenant,
            party=party,
            billing_account_id=billing_account_id,
            document_id=document_id,
            amount=amount,
            occurred_on=occurred_on,
            request_id=request_id,
            now=moment,
        ),
    )


def _deallocate_row(
    db: Session, *, scope: TenantScope, tenant_id: UUID, row: Any, code: str, at: datetime, key: str
) -> Decimal:
    """Reverse exactly this allocation row's remaining amount (a row of an opening-bearing draw is never here)."""
    remaining = allocation_remaining(db, scope=scope, allocation_id=row.id)
    if remaining <= 0:
        return Decimal(0)
    billing.deallocate_settlement(
        db,
        scope=scope,
        command=DeallocationCommand(
            allocation_id=row.id, amount=_money(remaining, code), occurred_at=at, source_ref=key
        ),
    )
    return remaining


def _is_opening_draw(db: Session, group_id: UUID) -> bool:
    group = db.get(bm.PostingGroup, group_id)
    return group is not None and group.group_kind == "opening_cash_applied"


def _reverse_draw_unit(
    db: Session,
    *,
    scope: TenantScope,
    tenant_id: UUID,
    group_id: UUID,
    document_id: UUID,
    code: str,
    at: datetime,
    key: str,
) -> Decimal:
    """An opening-bearing draw is ONE unit: billing restores it in its own cash-source order, so its whole remaining
    is reversed (a per-row amount would restore a different source)."""
    remaining = queries.draw_remaining(db, tenant_id, group_id, document_id)
    if remaining is None:
        raise ArRefused("draw_unreadable", "The draw's remaining amount cannot be read")
    if remaining <= 0:
        return Decimal(0)
    billing.reverse_cash_credit_draw(
        db,
        scope=scope,
        command=ReverseCashCreditDrawV1(
            scope=scope,
            draw_posting_group_id=group_id,
            amount=_money(remaining, code),
            authorization_ref="erp_v2.ar.finance",
            idempotency_key=key,
            occurred_at=at,
        ),
    )
    return remaining


def _reverse_draw(
    db: Session, *, tenant: Tenant, party: Party, group_id: UUID, document_id: UUID, occurred_on: date, now: datetime
) -> Outcome:
    scope = _scope(tenant)
    code = _functional(db, tenant.id)
    key = key_draw_reverse(group_id)
    if queries.posting_group_for_source(db, tenant.id, key) is not None:
        return Outcome(group_id, True, document_id=document_id)
    at = _action_time(db, tenant.id, occurred_on, now)
    if not _is_opening_draw(db, group_id):
        raise ArRefused("not_found", "The draw was not found")
    _require_other_person(db, tenant, party, _document_issuer(db, tenant.id, document_id))
    amount = _reverse_draw_unit(
        db, scope=scope, tenant_id=tenant.id, group_id=group_id, document_id=document_id, code=code, at=at, key=key
    )
    if amount == 0:
        raise ArRefused("nothing_to_deallocate", "This draw has already been reversed")
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(group_id),
        action=AUDIT_DEALLOCATED,
        success=True,
        details={"amount": str(amount), "document_id": str(document_id), "draw": True},
    )
    return Outcome(group_id, False, document_id=document_id)


def reverse_draw(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    group_id: UUID,
    document_id: UUID,
    occurred_on: date,
    now: datetime | None = None,
) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(group_id),
        action="reverse_draw",
        work=lambda: _reverse_draw(
            db,
            tenant=tenant,
            party=party,
            group_id=group_id,
            document_id=document_id,
            occurred_on=occurred_on,
            now=moment,
        ),
    )


def _deallocate(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    allocation_id: UUID,
    occurred_on: date,
    request_id: UUID,
    now: datetime,
) -> Outcome:
    scope = _scope(tenant)
    code = _functional(db, tenant.id)
    key = key_dealloc(allocation_id, request_id)
    if queries.posting_group_for_source(db, tenant.id, key) is not None:
        row_doc = db.scalar(
            select(bm.AllocationEffect.document_id).where(
                bm.AllocationEffect.tenant_id == tenant.id, bm.AllocationEffect.id == allocation_id
            )
        )
        return Outcome(allocation_id, True, document_id=row_doc)  # a replay is a no-op
    at = _action_time(db, tenant.id, occurred_on, now)
    row = db.scalar(
        select(bm.AllocationEffect).where(
            bm.AllocationEffect.tenant_id == tenant.id, bm.AllocationEffect.id == allocation_id
        )
    )
    if row is None or row.document_id is None or row.amount_delta <= 0:
        raise ArRefused("not_found", "The allocation was not found")
    if _is_opening_draw(db, row.posting_group_id):
        raise ArRefused(
            "opening_draw_unit",
            "This allocation is part of a draw that includes opening cash; reverse the whole draw from the invoice "
            "page",
        )
    _require_other_person(db, tenant, party, _document_issuer(db, tenant.id, row.document_id))
    amount = _deallocate_row(db, scope=scope, tenant_id=tenant.id, row=row, code=code, at=at, key=key)
    if amount == 0:
        raise ArRefused("nothing_to_deallocate", "This allocation has already been reversed")
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(allocation_id),
        action=AUDIT_DEALLOCATED,
        success=True,
        details={"amount": str(amount), "document_id": str(row.document_id)},
    )
    return Outcome(allocation_id, False, document_id=row.document_id)


def deallocate(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    allocation_id: UUID,
    occurred_on: date,
    request_id: UUID,
    now: datetime | None = None,
) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(allocation_id),
        action="deallocate",
        work=lambda: _deallocate(
            db,
            tenant=tenant,
            party=party,
            allocation_id=allocation_id,
            occurred_on=occurred_on,
            request_id=request_id,
            now=moment,
        ),
    )


def _reverse_receipt(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    receipt_draft_id: UUID,
    reason: str,
    occurred_on: date,
    now: datetime,
) -> Outcome:
    """Deallocate every live allocation of the receipt's settlement, then reverse the settlement's posting group."""
    scope = _scope(tenant)
    code = _functional(db, tenant.id)
    if reason not in REVERSAL_REASONS:
        raise ArRefused("reason_invalid", "The reason is void or bounced")
    draft = _locked_draft(db, tenant.id, receipt_draft_id)
    if draft.kind != KIND_RECEIPT or draft.billing_settlement_id is None:
        raise ArRefused("not_found", "The receipt was not found")
    settlement_id = draft.billing_settlement_id
    receipt = queries.receipt_for_settlement(db, tenant.id, settlement_id)
    if receipt is None:
        raise ArRefused("not_found", "The receipt was not found")
    _require_other_person(db, tenant, party, receipt.recorded_by)
    group_ref = f"settlement:{settlement_id}"
    group = queries.posting_group_for_source(db, tenant.id, group_ref)
    if group is None:
        raise ArRefused("not_found", "The settlement posting group was not found")
    reversed_already = db.scalar(
        select(bm.PostingGroup.id).where(
            bm.PostingGroup.tenant_id == tenant.id, bm.PostingGroup.reverses_group_id == group.id
        )
    )
    if reversed_already is not None:
        return Outcome(draft.id, True, settlement_id=settlement_id)
    at = _action_time(db, tenant.id, occurred_on, now)
    released = Decimal(0)
    draws: dict[UUID, UUID] = {}
    for row in queries.allocation_rows(db, tenant.id, settlement_id=settlement_id):
        if _is_opening_draw(db, row.posting_group_id):
            draws[row.posting_group_id] = row.document_id  # restored once, as a unit, below
            continue
        released += _deallocate_row(
            db,
            scope=scope,
            tenant_id=tenant.id,
            row=row,
            code=code,
            at=at,
            key=key_reverse_dealloc(settlement_id, row.id),
        )
    for group_id, document_id in draws.items():
        released += _reverse_draw_unit(
            db,
            scope=scope,
            tenant_id=tenant.id,
            group_id=group_id,
            document_id=document_id,
            code=code,
            at=at,
            key=key_reverse_draw(settlement_id, group_id),
        )
    billing.reverse_posting_group(
        db,
        scope=scope,
        command=ReversePostingGroupCommand(
            posting_group_id=group.id, occurred_at=at, source_ref=key_reverse(settlement_id)
        ),
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(settlement_id),
        action=AUDIT_RECEIPT_REVERSED,
        success=True,
        details={"reason": reason, "deallocated": str(released), "receipt_number": receipt.receipt_number},
    )
    return Outcome(draft.id, False, settlement_id=settlement_id)


def reverse_receipt(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    receipt_draft_id: UUID,
    reason: str,
    occurred_on: date,
    now: datetime | None = None,
) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(receipt_draft_id),
        action="reverse_receipt",
        work=lambda: _reverse_receipt(
            db,
            tenant=tenant,
            party=party,
            receipt_draft_id=receipt_draft_id,
            reason=reason,
            occurred_on=occurred_on,
            now=moment,
        ),
    )


def _void(db: Session, *, tenant: Tenant, party: Party, document_id: UUID, occurred_on: date, now: datetime) -> Outcome:
    scope = _scope(tenant)
    document = queries.billing_document(db, tenant.id, document_id)
    if document is None:
        raise ArRefused("not_found", "The document was not found")
    if queries.is_voided(db, tenant.id, document_id):
        return Outcome(document_id, True, document_id=document_id)  # a double POST is a no-op
    at = _action_time(db, tenant.id, occurred_on, now)
    _require_other_person(db, tenant, party, _document_issuer(db, tenant.id, document_id))
    billing.void_document(
        db,
        scope=scope,
        document_id=document_id,
        actor_ref=str(party.id),
        occurred_at=at,
        source_ref=key_void(document_id),
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(document_id),
        action=AUDIT_DOCUMENT_VOIDED,
        success=True,
        details={"kind": document.document_kind, "number": document.document_number},
    )
    return Outcome(document_id, False, document_id=document_id)


def void_document(
    db: Session, *, tenant: Tenant, party: Party, document_id: UUID, occurred_on: date, now: datetime | None = None
) -> Outcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(document_id),
        action="void",
        work=lambda: _void(
            db, tenant=tenant, party=party, document_id=document_id, occurred_on=occurred_on, now=moment
        ),
    )


def parse_decimal(value: str, *, label: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ArRefused("invalid_amount", f"Enter a number for {label}") from exc
