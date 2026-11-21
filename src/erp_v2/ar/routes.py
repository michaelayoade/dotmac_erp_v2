"""AR producer routes (B-16): server-rendered, under ``/finance/receivables``. No JSON API, no operator CLI.

Every POST is CSRF-protected (kernel middleware) and carries a hidden ``request_id`` where a retry must be a no-op.
With ``ar.producer.enabled`` false every POST refuses ``ar_producer_disabled`` and writes nothing; GETs work, so main
stays deployable. Permissions: ``erp.web.access`` to read, ``prepare`` to draft, ``post`` to issue / post / void / apply
/ deallocate / reverse. The acting party is ``require_web_permission``'s, never request data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID, uuid4

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.ar import constants, queries, service, settings, terms
from erp_v2.ar.models import KIND_CREDIT_NOTE, KIND_INVOICE, KIND_RECEIPT, STATUS_DRAFT
from erp_v2.ar.service import AllocationInput, ArRefused, LineInput
from erp_v2.journals import policy as journal_policy

ACCESS: Final = "erp.web.access"
PREPARE: Final = "erp.finance.receivables.prepare"
POST: Final = "erp.finance.receivables.post"
READ: Final = "erp.finance.receivables.read"
BASE: Final = "/finance/receivables"

ROUTE_PERMISSIONS: Final = {
    "drafts_list": ACCESS,
    "invoice_new": PREPARE,
    "invoice_create": PREPARE,
    "draft_detail": READ,
    "draw_reverse": POST,
    "draft_save": PREPARE,
    "draft_discard": PREPARE,
    "draft_issue": POST,
    "document_detail": ACCESS,
    "document_void": POST,
    "credit_note_new": PREPARE,
    "credit_note_create": PREPARE,
    "receipt_new": PREPARE,
    "receipt_create": PREPARE,
    "receipt_detail": ACCESS,
    "receipt_post": POST,
    "receipt_reverse": POST,
    "apply_credit": POST,
    "deallocate": POST,
}

router = APIRouter(tags=["receivables"])


def _today() -> date:
    return datetime.now(UTC).astimezone(terms.LAGOS).date()


def _ctx(request: Request, party: Party, **extra: object) -> dict[str, object]:
    return {
        **ui.template_globals(),
        "current_email": party.email,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "base": BASE,
        **extra,
    }


def _page(request: Request, template: str, context: dict[str, object], status_code: int = 200) -> HTMLResponse:
    response = render(request, f"erp_v2/receivables/{template}", context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _message(exc: ArRefused) -> str:
    return f"{exc.code}: {exc}"


def _uuid(value: str | None, label: str) -> UUID:
    try:
        return UUID((value or "").strip())
    except ValueError as exc:
        raise ArRefused("invalid_input", f"{label} is missing or invalid; reload the form") from exc


def _opt_uuid(value: str | None) -> UUID | None:
    value = (value or "").strip()
    return UUID(value) if value else None


def _date(value: str | None, label: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ArRefused("invalid_input", f"Enter {label} as YYYY-MM-DD") from exc


def _opt_date(value: str | None, label: str) -> date | None:
    return _date(value, label) if (value or "").strip() else None


def _form_items(form) -> list[tuple[str, str]]:
    return [(k, v) for k, v in form.multi_items() if isinstance(v, str) and k != "csrf_token"]


def _parse_lines(items: list[tuple[str, str]]) -> list[LineInput]:
    columns = {
        name: [v for k, v in items if k == name]
        for name in (
            "line_description",
            "line_quantity",
            "line_unit_amount",
            "line_revenue_account_id",
            "line_tax_code_id",
        )
    }
    rows = max((len(v) for v in columns.values()), default=0)

    def cell(name: str, i: int) -> str:
        values = columns[name]
        return values[i].strip() if i < len(values) else ""

    lines = []
    for i in range(rows):
        if not any(cell(name, i) for name in columns):
            continue  # a blank form row
        lines.append(
            LineInput(
                description=cell("line_description", i),
                quantity=service.parse_decimal(cell("line_quantity", i) or "1", label="quantity"),
                unit_amount=service.parse_decimal(cell("line_unit_amount", i), label="unit amount"),
                revenue_account_id=_opt_uuid(cell("line_revenue_account_id", i)),
                tax_code_id=_opt_uuid(cell("line_tax_code_id", i)),
            )
        )
    return lines


def _parse_allocations(items: list[tuple[str, str]]) -> list[AllocationInput]:
    found = []
    for key, value in items:
        if key.startswith("alloc_") and value.strip():
            found.append(
                AllocationInput(
                    _uuid(key.removeprefix("alloc_"), "invoice"), service.parse_decimal(value, label="allocation")
                )
            )
    return found


def _functional(db: Session, tenant: Tenant) -> tuple[str | None, str | None]:
    try:
        return journal_policy.functional_currency(db, tenant.id), None
    except journal_policy.FunctionalCurrencyError as exc:
        return None, str(exc)


# --- drafts -----------------------------------------------------------------------------------------------------


@router.get(f"{BASE}/drafts")
def drafts_list(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS)),
) -> HTMLResponse:
    return _page(request, "drafts.html", _ctx(request, party, drafts=queries.drafts(db, tenant.id)))


def _form_context(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    *,
    kind: str,
    draft=None,
    lines=(),
    original=None,
    customer_account_id: UUID | None = None,
    error: str | None = None,
) -> dict[str, object]:
    cfg = settings.load(db, tenant.id)
    currency, currency_error = _functional(db, tenant)
    customer_id = customer_account_id or (draft.customer_account_id if draft is not None else None)
    notice = queries.credit_note_notice(db, tenant.id, customer_id) if kind == KIND_CREDIT_NOTE and customer_id else []
    return _ctx(
        request,
        party,
        kind=kind,
        draft=draft,
        lines=list(lines),
        original=original,
        customer_account_id=customer_id,
        customers=queries.customers(db, tenant.id),
        revenue_accounts=queries.revenue_account_choices(db, tenant.id),
        tax_codes=queries.tax_codes(db, tenant.id),
        default_revenue=cfg.default_revenue_account,
        notice=notice,
        enabled=settings.producer_enabled(db, tenant.id),
        terms=cfg.terms,
        default_terms=service.default_terms_for_customer(db, tenant.id, customer_id, cfg),
        currency=currency,
        currency_error=currency_error,
        request_id=str(uuid4()),
        today=_today().isoformat(),
        error=error,
    )


@router.get(f"{BASE}/invoices/new")
def invoice_new(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _page(request, "invoice_form.html", _form_context(request, db, tenant, party, kind=KIND_INVOICE))


@router.post(f"{BASE}/invoices/new", response_model=None)
async def invoice_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    items = _form_items(await request.form())
    values = dict(items)
    try:
        lines = _parse_lines(items)
        outcome = service.create_draft(
            db,
            tenant=tenant,
            party=party,
            request_id=_uuid(values.get("request_id"), "The form"),
            kind=KIND_INVOICE,
            customer_account_id=_uuid(values.get("customer_account_id"), "The customer"),
            document_date=_date(values.get("document_date"), "the invoice date"),
            lines=lines,
            payment_terms_code=values.get("payment_terms_code") or None,
            due_date=_opt_date(values.get("due_date"), "the due date"),
        )
    except ArRefused as exc:
        return _page(
            request,
            "invoice_form.html",
            _form_context(request, db, tenant, party, kind=KIND_INVOICE, error=_message(exc)),
            400,
        )
    return _redirect(f"{BASE}/drafts/{outcome.id}")


def _draft_or_404(db: Session, tenant: Tenant, draft_id: UUID):
    draft = queries.draft(db, tenant.id, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="The draft was not found")
    return draft


def _draft_page(
    request: Request, db: Session, tenant: Tenant, party: Party, draft_id: UUID, *, error: str | None = None
) -> HTMLResponse:
    draft = _draft_or_404(db, tenant, draft_id)
    lines = queries.draft_lines(db, tenant.id, draft.id)
    if draft.kind == KIND_RECEIPT:
        return _receipt_page(request, db, tenant, party, draft_id, error=error)
    original = (
        queries.billing_document(db, tenant.id, draft.original_document_id) if draft.original_document_id else None
    )
    template = "credit_note_form.html" if draft.kind == KIND_CREDIT_NOTE else "invoice_form.html"
    return _page(
        request,
        template,
        _form_context(
            request, db, tenant, party, kind=draft.kind, draft=draft, lines=lines, original=original, error=error
        ),
        400 if error else 200,
    )


@router.get(BASE + "/drafts/{draft_id}")
def draft_detail(
    draft_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(READ)),
) -> HTMLResponse:
    return _draft_page(request, db, tenant, party, draft_id)


@router.post(BASE + "/drafts/{draft_id}", response_model=None)
async def draft_save(
    draft_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    draft = _draft_or_404(db, tenant, draft_id)
    items = _form_items(await request.form())
    values = dict(items)
    try:
        service.update_draft(
            db,
            tenant=tenant,
            party=party,
            draft_id=draft_id,
            customer_account_id=_uuid(values.get("customer_account_id"), "The customer"),
            document_date=_date(values.get("document_date"), "the date"),
            lines=_parse_lines(items) if draft.kind != KIND_RECEIPT else [],
            allocations=_parse_allocations(items) if draft.kind == KIND_RECEIPT else [],
            original_document_id=draft.original_document_id,
            bank_account_id=_opt_uuid(values.get("bank_account_id")) or draft.bank_account_id,
            bank_reference=values.get("bank_reference") or draft.bank_reference,
            amount=service.parse_decimal(values["amount"], label="amount") if values.get("amount") else draft.amount,
            reason=values.get("reason") or draft.reason,
            payment_terms_code=(values.get("payment_terms_code") or None) if draft.kind == KIND_INVOICE else None,
            due_date=_opt_date(values.get("due_date"), "the due date") if draft.kind == KIND_INVOICE else None,
        )
    except ArRefused as exc:
        return _draft_page(request, db, tenant, party, draft_id, error=_message(exc))
    return _redirect(f"{BASE}/drafts/{draft_id}")


@router.post(BASE + "/drafts/{draft_id}/discard", response_model=None)
def draft_discard(
    draft_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    try:
        service.discard_draft(db, tenant=tenant, party=party, draft_id=draft_id)
    except ArRefused as exc:
        return _draft_page(request, db, tenant, party, draft_id, error=_message(exc))
    return _redirect(f"{BASE}/drafts")


@router.post(BASE + "/drafts/{draft_id}/issue", response_model=None)
def draft_issue(
    draft_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(POST)),
) -> HTMLResponse | RedirectResponse:
    try:
        outcome = service.issue_draft(db, tenant=tenant, party=party, draft_id=draft_id)
    except ArRefused as exc:
        return _draft_page(request, db, tenant, party, draft_id, error=_message(exc))
    return _redirect(f"{BASE}/documents/{outcome.document_id}")


# --- documents --------------------------------------------------------------------------------------------------


def _document_page(
    request: Request, db: Session, tenant: Tenant, party: Party, document_id: UUID, *, error: str | None = None
) -> HTMLResponse:
    document = queries.billing_document(db, tenant.id, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="The document was not found")
    account = queries.billing_account(db, tenant.id, document.billing_account_id)
    exposure = queries.exposure(db, tenant.id, document.id) if document.document_kind == "invoice" else None
    return _page(
        request,
        "document.html",
        _ctx(
            request,
            party,
            document=document,
            account=account,
            lines=queries.billing_lines(db, tenant.id, document.id),
            line_accounts={la.line_number: la for la in queries.line_accounts(db, tenant.id, document.id)},
            exposure=exposure,
            allocations=queries.allocation_rows(db, tenant.id, document_id=document.id),
            credit_notes=queries.credit_notes_for(db, tenant.id, document.id),
            voided=queries.is_voided(db, tenant.id, document.id),
            draws=queries.opening_draws(db, tenant.id, document.id) if document.document_kind == "invoice" else [],
            gl_status=queries.gl_status(db, tenant.id, document.id),
            request_id=str(uuid4()),
            today=_today().isoformat(),
            error=error,
        ),
        400 if error else 200,
    )


@router.get(BASE + "/documents/{document_id}")
def document_detail(
    document_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS)),
) -> HTMLResponse:
    return _document_page(request, db, tenant, party, document_id)


@router.post(BASE + "/documents/{document_id}/void", response_model=None)
async def document_void(
    document_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(POST)),
) -> HTMLResponse | RedirectResponse:
    values = dict(_form_items(await request.form()))
    try:
        service.void_document(
            db,
            tenant=tenant,
            party=party,
            document_id=document_id,
            occurred_on=_date(values.get("occurred_on"), "the void date"),
        )
    except ArRefused as exc:
        return _document_page(request, db, tenant, party, document_id, error=_message(exc))
    return _redirect(f"{BASE}/documents/{document_id}")


# --- credit notes -----------------------------------------------------------------------------------------------


@router.get(f"{BASE}/credit-notes/new")
def credit_note_new(
    request: Request,
    invoice: UUID | None = None,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    original = queries.billing_document(db, tenant.id, invoice) if invoice else None
    account = queries.billing_account(db, tenant.id, original.billing_account_id) if original else None
    customer_id = UUID(account.external_account_ref) if account and _is_uuid(account.external_account_ref) else None
    lines = []
    if original is not None:
        by_line = {la.line_number: la for la in queries.line_accounts(db, tenant.id, original.id)}
        lines = [
            LineInput(
                line.description,
                line.quantity,
                line.unit_amount,
                by_line[line.line_number].revenue_account_id if line.line_number in by_line else None,
                None,
            )
            for line in queries.billing_lines(db, tenant.id, original.id)
        ]
    return _page(
        request,
        "credit_note_form.html",
        _form_context(
            request,
            db,
            tenant,
            party,
            kind=KIND_CREDIT_NOTE,
            original=original,
            lines=lines,
            customer_account_id=customer_id,
        ),
    )


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


@router.post(f"{BASE}/credit-notes/new", response_model=None)
async def credit_note_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    items = _form_items(await request.form())
    values = dict(items)
    try:
        original_id = _uuid(values.get("original_document_id"), "The invoice")
        outcome = service.create_draft(
            db,
            tenant=tenant,
            party=party,
            request_id=_uuid(values.get("request_id"), "The form"),
            kind=KIND_CREDIT_NOTE,
            customer_account_id=_uuid(values.get("customer_account_id"), "The customer"),
            document_date=_date(values.get("document_date"), "the credit note date"),
            lines=_parse_lines(items),
            original_document_id=original_id,
            reason=values.get("reason"),
        )
    except ArRefused as exc:
        original = _maybe_document(db, tenant, values.get("original_document_id"))
        return _page(
            request,
            "credit_note_form.html",
            _form_context(request, db, tenant, party, kind=KIND_CREDIT_NOTE, original=original, error=_message(exc)),
            400,
        )
    return _redirect(f"{BASE}/drafts/{outcome.id}")


def _maybe_document(db: Session, tenant: Tenant, raw: str | None):
    try:
        return queries.billing_document(db, tenant.id, UUID((raw or "").strip()))
    except ValueError:
        return None


# --- receipts ---------------------------------------------------------------------------------------------------


def _receipt_form_page(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    *,
    draft=None,
    lines=(),
    customer_account_id: UUID | None = None,
    error: str | None = None,
) -> HTMLResponse:
    currency, currency_error = _functional(db, tenant)
    customer_id = customer_account_id or (draft.customer_account_id if draft is not None else None)
    open_invoices = []
    if customer_id is not None and currency is not None:
        account = queries.billing_account_for_customer(db, tenant.id, customer_id, currency)
        if account is not None:
            open_invoices = queries.open_invoices(db, tenant.id, account.id)
    return _page(
        request,
        "receipt_form.html",
        _ctx(
            request,
            party,
            draft=draft,
            allocations={row.target_document_id: row.amount for row in lines if row.target_document_id},
            customer_account_id=customer_id,
            customers=queries.customers(db, tenant.id),
            bank_accounts=queries.bank_accounts(db, tenant.id, currency),
            open_invoices=open_invoices,
            enabled=settings.producer_enabled(db, tenant.id),
            currency=currency,
            currency_error=currency_error,
            request_id=str(uuid4()),
            today=_today().isoformat(),
            error=error,
        ),
        400 if error else 200,
    )


@router.get(f"{BASE}/receipts/new")
def receipt_new(
    request: Request,
    customer: UUID | None = None,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _receipt_form_page(request, db, tenant, party, customer_account_id=customer)


@router.post(f"{BASE}/receipts/new", response_model=None)
async def receipt_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    items = _form_items(await request.form())
    values = dict(items)
    customer_id = _opt_uuid(values.get("customer_account_id"))
    try:
        outcome = service.create_draft(
            db,
            tenant=tenant,
            party=party,
            request_id=_uuid(values.get("request_id"), "The form"),
            kind=KIND_RECEIPT,
            customer_account_id=_uuid(values.get("customer_account_id"), "The customer"),
            document_date=_date(values.get("document_date"), "the receipt date"),
            allocations=_parse_allocations(items),
            bank_account_id=_uuid(values.get("bank_account_id"), "The bank account"),
            bank_reference=values.get("bank_reference"),
            amount=service.parse_decimal(values.get("amount", ""), label="the amount received"),
        )
    except ArRefused as exc:
        return _receipt_form_page(request, db, tenant, party, customer_account_id=customer_id, error=_message(exc))
    return _redirect(f"{BASE}/receipts/{outcome.id}")


def _receipt_page(
    request: Request, db: Session, tenant: Tenant, party: Party, draft_id: UUID, *, error: str | None = None
) -> HTMLResponse:
    draft = _draft_or_404(db, tenant, draft_id)
    if draft.kind != KIND_RECEIPT:
        raise HTTPException(status_code=404, detail="The receipt was not found")
    if draft.status == STATUS_DRAFT:
        return _receipt_form_page(
            request, db, tenant, party, draft=draft, lines=queries.draft_lines(db, tenant.id, draft.id), error=error
        )
    receipt = queries.receipt_for_draft(db, tenant.id, draft.id)
    rows = queries.allocation_rows(db, tenant.id, settlement_id=draft.billing_settlement_id) if receipt else []
    reversed_ = False
    if receipt is not None:
        group = queries.posting_group_for_source(db, tenant.id, f"settlement:{receipt.billing_settlement_id}")
        reversed_ = group is not None and queries.group_reversed(db, tenant.id, group.id)
    return _page(
        request,
        "receipt.html",
        _ctx(
            request,
            party,
            draft=draft,
            receipt=receipt,
            allocations=rows,
            reversed=reversed_,
            customer=queries.customer(db, tenant.id, draft.customer_account_id),
            request_id=str(uuid4()),
            today=_today().isoformat(),
            error=error,
        ),
        400 if error else 200,
    )


@router.get(BASE + "/receipts/{draft_id}")
def receipt_detail(
    draft_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS)),
) -> HTMLResponse:
    return _receipt_page(request, db, tenant, party, draft_id)


@router.post(BASE + "/receipts/{draft_id}/post", response_model=None)
def receipt_post(
    draft_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(POST)),
) -> HTMLResponse | RedirectResponse:
    try:
        service.post_receipt(db, tenant=tenant, party=party, draft_id=draft_id)
    except ArRefused as exc:
        return _receipt_page(request, db, tenant, party, draft_id, error=_message(exc))
    return _redirect(f"{BASE}/receipts/{draft_id}")


@router.post(BASE + "/receipts/{draft_id}/reverse", response_model=None)
async def receipt_reverse(
    draft_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(POST)),
) -> HTMLResponse | RedirectResponse:
    values = dict(_form_items(await request.form()))
    try:
        service.reverse_receipt(
            db,
            tenant=tenant,
            party=party,
            receipt_draft_id=draft_id,
            reason=values.get("reason", ""),
            occurred_on=_date(values.get("occurred_on"), "the reversal date"),
        )
    except ArRefused as exc:
        return _receipt_page(request, db, tenant, party, draft_id, error=_message(exc))
    return _redirect(f"{BASE}/receipts/{draft_id}")


# --- apply credit / deallocate ----------------------------------------------------------------------------------


@router.post(BASE + "/accounts/{billing_account_id}/apply-credit", response_model=None)
async def apply_credit(
    billing_account_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(POST)),
) -> HTMLResponse | RedirectResponse:
    values = dict(_form_items(await request.form()))
    document_id = _maybe_uuid(values.get("document_id"))
    try:
        service.apply_credit(
            db,
            tenant=tenant,
            party=party,
            billing_account_id=billing_account_id,
            document_id=_uuid(values.get("document_id"), "The invoice"),
            amount=service.parse_decimal(values.get("amount", ""), label="the amount"),
            occurred_on=_date(values.get("occurred_on"), "the date"),
            request_id=_uuid(values.get("request_id"), "The form"),
        )
    except ArRefused as exc:
        if document_id is None:
            raise HTTPException(status_code=400, detail=_message(exc)) from exc
        return _document_page(request, db, tenant, party, document_id, error=_message(exc))
    return _redirect(f"{BASE}/documents/{document_id}")


def _maybe_uuid(raw: str | None) -> UUID | None:
    try:
        return UUID((raw or "").strip())
    except ValueError:
        return None


@router.post(BASE + "/draws/{group_id}/reverse", response_model=None)
async def draw_reverse(
    group_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(POST)),
) -> HTMLResponse | RedirectResponse:
    values = dict(_form_items(await request.form()))
    document_id = _maybe_uuid(values.get("document_id"))
    try:
        service.reverse_draw(
            db,
            tenant=tenant,
            party=party,
            group_id=group_id,
            document_id=_uuid(values.get("document_id"), "The invoice"),
            occurred_on=_date(values.get("occurred_on"), "the date"),
        )
    except ArRefused as exc:
        if document_id is None:
            raise HTTPException(status_code=400, detail=_message(exc)) from exc
        return _document_page(request, db, tenant, party, document_id, error=_message(exc))
    return _redirect(f"{BASE}/documents/{document_id}")


@router.post(BASE + "/allocations/{allocation_id}/deallocate", response_model=None)
async def deallocate(
    allocation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(POST)),
) -> HTMLResponse | RedirectResponse:
    values = dict(_form_items(await request.form()))
    document_id = _maybe_uuid(values.get("document_id"))
    try:
        service.deallocate(
            db,
            tenant=tenant,
            party=party,
            allocation_id=allocation_id,
            occurred_on=_date(values.get("occurred_on"), "the date"),
            request_id=_uuid(values.get("request_id"), "The form"),
        )
    except ArRefused as exc:
        if document_id is None:
            raise HTTPException(status_code=400, detail=_message(exc)) from exc
        return _document_page(request, db, tenant, party, document_id, error=_message(exc))
    return _redirect(f"{BASE}/documents/{document_id}" if document_id else BASE)


receivables_producer_feature = FeatureManifest(
    name="erp_receivables_producer",
    web_routers=(router,),
    audit_actions=service.AUDIT_ACTIONS,
    setting_domains=(str(settings.DOMAIN),),
    obligation_sources=(constants.SOURCE_KIND,),
)
