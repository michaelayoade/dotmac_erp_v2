"""Supplier payment routes (B3). Every route is permission-gated and dormant until a named party
holds the role (access_cli); admin and staff hold none of these permissions (W14).

Four surfaces, one per power, so a role never sees another role's actions:

- prepare   (finance_preparer)  /finance/supplier-payments            propose from open obligations
- approve   (finance_approver)  /finance/supplier-payment-approvals   approve or cancel; reversal of a
                                                                      recorded payment (approver power:
                                                                      payables refuses the approver and
                                                                      the recorder as reverser)
- treasury  (treasury)          /finance/supplier-payment-treasury    record as paid with the bank
                                                                      evidence reference; optional bank file
- reversal  (finance_approver)  /finance/supplier-payment-reversals

None of these are facet entry routes: the facet admission permission (erp.web.access) also applies, so
a finance role without staff is refused. Nothing here calls a bank or provider.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID, uuid4

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from dotmac_payables.contracts import SupplierPaymentStatus
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.disbursement import file as bank_file
from erp_v2.disbursement import policy as disbursement_policy
from erp_v2.journals import policy as journal_policy
from erp_v2.supplier_payments import queries, service
from erp_v2.supplier_payments.service import AllocationRequest, PaymentRefused

PREPARE: Final = "erp.finance.supplier_payments.prepare"
APPROVE: Final = "erp.finance.supplier_payments.approve"
RECORD: Final = "erp.treasury.supplier_payments.record"
GENERATE: Final = "erp.treasury.disbursement_files.generate"

PREPARE_BASE: Final = "/finance/supplier-payments"
APPROVE_BASE: Final = "/finance/supplier-payment-approvals"
TREASURY_BASE: Final = "/finance/supplier-payment-treasury"
REVERSAL_BASE: Final = "/finance/supplier-payment-reversals"

# route name -> permission, asserted against the access profile in tests.
ROUTE_PERMISSIONS: Final = {
    "register": PREPARE,
    "new": PREPARE,
    "create": PREPARE,
    "prepare_detail": PREPARE,
    "approval_queue": APPROVE,
    "approval_detail": APPROVE,
    "approve": APPROVE,
    "cancel": APPROVE,
    "reversal_queue": APPROVE,
    "reversal_detail": APPROVE,
    "reverse": APPROVE,
    "treasury_queue": RECORD,
    "treasury_detail": RECORD,
    "record": RECORD,
    "bank_file": GENERATE,
}

router = APIRouter(tags=["supplier-payments"])


def _ctx(request: Request, party: Party, **extra: object) -> dict[str, object]:
    return {
        **ui.template_globals(),
        "current_email": party.email,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        **extra,
    }


def _page(request: Request, template: str, context: dict[str, object], status_code: int = 200) -> HTMLResponse:
    response = render(request, template, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _message(exc: PaymentRefused) -> str:
    return f"{exc.code}: {exc}"


def _functional(db: Session, tenant: Tenant) -> tuple[str | None, str | None]:
    try:
        return journal_policy.functional_currency(db, tenant.id), None
    except journal_policy.FunctionalCurrencyError as exc:
        return None, str(exc)


def _list_page(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    *,
    title: str,
    detail_base: str,
    statuses: tuple[SupplierPaymentStatus, ...] | None,
    extra: dict[str, object] | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return _page(
        request,
        "supplier_payments/list.html",
        _ctx(
            request,
            party,
            title=title,
            detail_base=detail_base,
            rows=queries.register(db, tenant_id=tenant.id, statuses=statuses),
            error=error,
            **(extra or {}),
        ),
        status_code=400 if error else 200,
    )


def _detail_page(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    payment_id: UUID,
    *,
    mode: str,
    back: str,
    error: str | None = None,
) -> HTMLResponse:
    data = queries.detail(db, tenant_id=tenant.id, payment_id=payment_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Supplier payment was not found")
    return _page(
        request,
        "supplier_payments/detail.html",
        _ctx(
            request,
            party,
            **data,
            mode=mode,
            back=back,
            bases={"approve": APPROVE_BASE, "treasury": TREASURY_BASE, "reverse": REVERSAL_BASE},
            bank_file_enabled=disbursement_policy.bank_file_generation_enabled(db, tenant.id),
            reversal_default=date.today().isoformat(),
            error=error,
        ),
        status_code=400 if error else 200,
    )


async def _values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str) and key != "csrf_token"}


# --- preparer --------------------------------------------------------------


@router.get(PREPARE_BASE)
def register(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _list_page(
        request,
        db,
        tenant,
        party,
        title="Supplier payments",
        detail_base=PREPARE_BASE,
        statuses=None,
        extra={"new_link": f"{PREPARE_BASE}/new"},
    )


def _new_context(
    request: Request, db: Session, tenant: Tenant, party: Party, *, error: str | None = None
) -> dict[str, object]:
    currency, currency_error = _functional(db, tenant)
    return _ctx(
        request,
        party,
        currency=currency,
        currency_error=currency_error,
        accounts=queries.bank_accounts(db, tenant_id=tenant.id, currency=currency) if currency else [],
        obligations=queries.open_obligations(db, tenant_id=tenant.id, currency=currency) if currency else [],
        request_id=str(uuid4()),
        today=date.today().isoformat(),
        error=error,
    )


@router.get(f"{PREPARE_BASE}/new")
def new(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _page(request, "supplier_payments/new.html", _new_context(request, db, tenant, party))


def _parse_create(form_items: list[tuple[str, str]]) -> tuple[UUID, UUID, date, list[AllocationRequest]]:
    values = dict(form_items)
    try:
        request_id = UUID(values.get("request_id", ""))
        bank_account_id = UUID(values.get("bank_account_id", ""))
    except ValueError as exc:
        raise PaymentRefused("invalid_input", "The form has expired or is incomplete; reload it") from exc
    try:
        value_date = date.fromisoformat(values.get("value_date", ""))
    except ValueError as exc:
        raise PaymentRefused("invalid_input", "Enter the value date as YYYY-MM-DD") from exc
    allocations: list[AllocationRequest] = []
    for key, raw_id in form_items:
        if key != "obligation_id":
            continue
        try:
            obligation_id = UUID(raw_id)
        except ValueError as exc:
            raise PaymentRefused("invalid_input", "An obligation reference is invalid") from exc
        raw_amount = values.get(f"amount_{obligation_id}", "").strip()
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation as exc:
            raise PaymentRefused("invalid_amount", "Enter an amount for every selected obligation") from exc
        allocations.append(AllocationRequest(obligation_id, amount))
    return request_id, bank_account_id, value_date, allocations


@router.post(PREPARE_BASE, response_model=None)
async def create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    items = [(k, v) for k, v in form.multi_items() if isinstance(v, str) and k != "csrf_token"]
    try:
        request_id, bank_account_id, value_date, allocations = _parse_create(items)
        outcome = service.prepare_payment(
            db,
            tenant=tenant,
            party=party,
            request_id=request_id,
            bank_account_id=bank_account_id,
            value_date=value_date,
            allocations=allocations,
        )
    except PaymentRefused as exc:
        return _page(
            request, "supplier_payments/new.html", _new_context(request, db, tenant, party, error=_message(exc)), 400
        )
    return _redirect(f"{PREPARE_BASE}/{outcome.payment_id}")


@router.get(PREPARE_BASE + "/{payment_id}")
def prepare_detail(
    payment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, payment_id, mode="prepare", back=PREPARE_BASE)


# --- approver --------------------------------------------------------------


@router.get(APPROVE_BASE)
def approval_queue(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _list_page(
        request,
        db,
        tenant,
        party,
        title="Supplier payments awaiting approval",
        detail_base=APPROVE_BASE,
        statuses=(SupplierPaymentStatus.DRAFT, SupplierPaymentStatus.APPROVED),
    )


@router.get(APPROVE_BASE + "/{payment_id}")
def approval_detail(
    payment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, payment_id, mode="approve", back=APPROVE_BASE)


@router.post(APPROVE_BASE + "/{payment_id}/approve", response_model=None)
def approve(
    payment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse | RedirectResponse:
    try:
        service.approve_payment(db, tenant=tenant, party=party, payment_id=payment_id)
    except PaymentRefused as exc:
        return _detail_page(
            request, db, tenant, party, payment_id, mode="approve", back=APPROVE_BASE, error=_message(exc)
        )
    return _redirect(f"{APPROVE_BASE}/{payment_id}")


@router.post(APPROVE_BASE + "/{payment_id}/cancel", response_model=None)
async def cancel(
    payment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse | RedirectResponse:
    values = await _values(request)
    try:
        service.cancel_payment(db, tenant=tenant, party=party, payment_id=payment_id, reason=values.get("reason", ""))
    except PaymentRefused as exc:
        return _detail_page(
            request, db, tenant, party, payment_id, mode="approve", back=APPROVE_BASE, error=_message(exc)
        )
    return _redirect(f"{APPROVE_BASE}/{payment_id}")


# --- reversal (approver power) ---------------------------------------------


@router.get(REVERSAL_BASE)
def reversal_queue(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _list_page(
        request,
        db,
        tenant,
        party,
        title="Recorded supplier payments (reversal)",
        detail_base=REVERSAL_BASE,
        statuses=(SupplierPaymentStatus.RECORDED, SupplierPaymentStatus.REVERSED),
    )


@router.get(REVERSAL_BASE + "/{payment_id}")
def reversal_detail(
    payment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, payment_id, mode="reverse", back=REVERSAL_BASE)


@router.post(REVERSAL_BASE + "/{payment_id}/reverse", response_model=None)
async def reverse(
    payment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse | RedirectResponse:
    values = await _values(request)
    try:
        try:
            reversal_date = date.fromisoformat(values.get("reversal_date", ""))
        except ValueError as exc:
            raise PaymentRefused("invalid_input", "Enter the reversal date as YYYY-MM-DD") from exc
        service.reverse_payment(
            db,
            tenant=tenant,
            party=party,
            payment_id=payment_id,
            reason=values.get("reason", ""),
            reversal_date=reversal_date,
        )
    except PaymentRefused as exc:
        return _detail_page(
            request, db, tenant, party, payment_id, mode="reverse", back=REVERSAL_BASE, error=_message(exc)
        )
    return _redirect(f"{REVERSAL_BASE}/{payment_id}")


# --- treasury --------------------------------------------------------------


@router.get(TREASURY_BASE)
def treasury_queue(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(RECORD)),
) -> HTMLResponse:
    return _list_page(
        request,
        db,
        tenant,
        party,
        title="Approved supplier payments to record",
        detail_base=TREASURY_BASE,
        statuses=(SupplierPaymentStatus.APPROVED,),
        extra={
            "bank_file_enabled": disbursement_policy.bank_file_generation_enabled(db, tenant.id),
            "bank_file_action": f"{TREASURY_BASE}/bank-file",
        },
    )


@router.get(TREASURY_BASE + "/{payment_id}")
def treasury_detail(
    payment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(RECORD)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, payment_id, mode="treasury", back=TREASURY_BASE)


@router.post(TREASURY_BASE + "/{payment_id}/record", response_model=None)
async def record(
    payment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(RECORD)),
) -> HTMLResponse | RedirectResponse:
    values = await _values(request)
    try:
        service.record_payment(
            db, tenant=tenant, party=party, payment_id=payment_id, bank_reference=values.get("bank_reference", "")
        )
    except PaymentRefused as exc:
        return _detail_page(
            request, db, tenant, party, payment_id, mode="treasury", back=TREASURY_BASE, error=_message(exc)
        )
    return _redirect(f"{TREASURY_BASE}/{payment_id}")


@router.post(TREASURY_BASE + "/bank-file", response_model=None)
async def bank_file_download(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(GENERATE)),
) -> Response | HTMLResponse:
    form = await request.form()
    try:
        ids = [UUID(v) for k, v in form.multi_items() if k == "payment_id" and isinstance(v, str)]
    except ValueError:
        ids = []
    try:
        built = service.generate_bank_file(db, tenant=tenant, party=party, payment_ids=ids)
    except PaymentRefused as exc:
        if exc.code == "bank_file_disabled":
            raise HTTPException(status_code=404, detail="Not found") from exc
        return _list_page(
            request,
            db,
            tenant,
            party,
            title="Approved supplier payments to record",
            detail_base=TREASURY_BASE,
            statuses=(SupplierPaymentStatus.APPROVED,),
            extra={"bank_file_enabled": True, "bank_file_action": f"{TREASURY_BASE}/bank-file"},
            error=_message(exc),
        )
    # The receipt and audit row must be durable before the bytes leave the process.
    db.commit()
    return Response(
        content=built.file.content,
        media_type="text/csv; charset=utf-8",
        headers=bank_file.no_store_headers(built.filename),
    )


supplier_payments_feature = FeatureManifest(
    name="erp_supplier_payments",
    web_routers=(router,),
    audit_actions=service.AUDIT_ACTIONS,
    setting_domains=(str(disbursement_policy.TREASURY_POLICY_DOMAIN),),
)
