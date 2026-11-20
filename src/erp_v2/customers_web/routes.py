"""Customer screen routes (F-F): server-rendered, under ``/finance/customers``. No JSON API.

Every POST is CSRF-protected (kernel middleware). Create carries a hidden ``request_id`` (kernel idempotency). Reads
need ``erp.web.access``; every write needs ``erp.finance.customers.manage`` (finance_preparer). The acting party is
``require_web_permission``'s, never request data. No feature flag: the routes are dormant until a party holds the
permission, and a refusal writes nothing.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid4

from dotmac_customers.contracts import AccountStatus
from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.customers_web import queries, service, settings
from erp_v2.customers_web.service import CustomerRefused, FinanceInput

ACCESS: Final = "erp.web.access"
MANAGE: Final = "erp.finance.customers.manage"
BASE: Final = "/finance/customers"

ROUTE_PERMISSIONS: Final = {
    "customers_list": ACCESS,
    "customer_new": MANAGE,
    "customer_create": MANAGE,
    "customer_detail": ACCESS,
    "customer_edit": MANAGE,
    "customer_save": MANAGE,
    "customer_status": MANAGE,
}

router = APIRouter(tags=["customers"])


def _ctx(request: Request, party: Party, **extra: object) -> dict[str, object]:
    return {
        **ui.template_globals(),
        "current_email": party.email,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "base": BASE,
        **extra,
    }


def _page(request: Request, template: str, context: dict[str, object], status_code: int = 200) -> HTMLResponse:
    response = render(request, f"erp_v2/customers/{template}", context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _message(exc: CustomerRefused) -> str:
    return f"{exc.code}: {exc}"


def _uuid(value: str | None, label: str) -> UUID:
    try:
        return UUID((value or "").strip())
    except ValueError as exc:
        raise CustomerRefused("invalid_input", f"{label} is missing or invalid; reload the form") from exc


def _form(form) -> dict[str, str]:
    return {k: v for k, v in form.multi_items() if isinstance(v, str) and k != "csrf_token"}


def _finance(values: dict[str, str]) -> FinanceInput:
    return FinanceInput(**{name: values.get(name) for name in service.FINANCE_FIELDS})


def _form_context(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    *,
    account=None,
    values: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    if values is None:
        row = queries.finance(db, tenant.id, account.id) if account is not None else None
        values = {name: (getattr(row, name) or "") if row else "" for name in service.FINANCE_FIELDS}
        values["display_name"] = account.display_name if account is not None else ""
        values["notes"] = (queries.notes(db, tenant.id, account.id) or "") if account is not None else ""
    return _ctx(
        request,
        party,
        account=account,
        values=values,
        terms=sorted(service.valid_terms(db, tenant.id)),
        series=settings.series_code(db, tenant.id),
        rename_available=service.rename_available(),
        statuses=[s.value for s in service.CREATE_STATUSES],
        request_id=str(uuid4()),
        error=error,
    )


@router.get(BASE)
def customers_list(
    request: Request,
    q: str = "",
    status: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS)),
) -> HTMLResponse:
    status = status if status in {s.value for s in AccountStatus} else ""
    page = max(page, 1)
    rows, total = queries.listing(db, tenant.id, search=q, status=status, page=page)
    return _page(
        request,
        "list.html",
        _ctx(
            request,
            party,
            rows=rows,
            total=total,
            q=q,
            status=status,
            page=page,
            pages=max(-(-total // queries.PAGE_SIZE), 1),
            all_statuses=[s.value for s in AccountStatus],
        ),
    )


@router.get(BASE + "/new")
def customer_new(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(MANAGE)),
) -> HTMLResponse:
    return _page(request, "form.html", _form_context(request, db, tenant, party))


@router.post(BASE + "/new", response_model=None)
async def customer_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(MANAGE)),
) -> HTMLResponse | RedirectResponse:
    values = _form(await request.form())
    try:
        outcome = service.create_customer(
            db,
            tenant=tenant,
            party=party,
            request_id=_uuid(values.get("request_id"), "The form"),
            account_number=values.get("account_number"),
            display_name=values.get("display_name"),
            status=_status(values.get("status") or AccountStatus.ACTIVE.value),
            notes=values.get("notes"),
            finance=_finance(values),
        )
    except CustomerRefused as exc:
        return _page(
            request, "form.html", _form_context(request, db, tenant, party, values=values, error=_message(exc)), 400
        )
    return _redirect(f"{BASE}/{outcome.account_id}")


def _status(value: str) -> AccountStatus:
    try:
        return AccountStatus(value)
    except ValueError as exc:
        raise CustomerRefused("invalid_input", "Choose a valid status") from exc


def _account_or_404(db: Session, tenant: Tenant, account_id: UUID):
    account = queries.account(db, tenant.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="The customer was not found")
    return account


def _detail_page(
    request: Request, db: Session, tenant: Tenant, party: Party, account_id: UUID, *, error: str | None = None
) -> HTMLResponse:
    account = _account_or_404(db, tenant, account_id)
    billing = queries.billing_accounts(db, tenant.id, account.id)
    return _page(
        request,
        "detail.html",
        _ctx(
            request,
            party,
            account=account,
            finance=queries.finance(db, tenant.id, account.id),
            notes=queries.notes(db, tenant.id, account.id),
            billing_accounts=billing,
            open_items=queries.open_item_count(db, tenant.id, billing),
            error=error,
        ),
        400 if error else 200,
    )


@router.get(BASE + "/{account_id}")
def customer_detail(
    account_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, account_id)


@router.get(BASE + "/{account_id}/edit")
def customer_edit(
    account_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(MANAGE)),
) -> HTMLResponse:
    account = _account_or_404(db, tenant, account_id)
    return _page(request, "form.html", _form_context(request, db, tenant, party, account=account))


@router.post(BASE + "/{account_id}/edit", response_model=None)
async def customer_save(
    account_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(MANAGE)),
) -> HTMLResponse | RedirectResponse:
    account = _account_or_404(db, tenant, account_id)
    values = _form(await request.form())
    try:
        service.update_customer(
            db,
            tenant=tenant,
            party=party,
            account_id=account_id,
            display_name=values.get("display_name"),
            notes=values.get("notes"),
            finance=_finance(values),
        )
    except CustomerRefused as exc:
        return _page(
            request,
            "form.html",
            _form_context(request, db, tenant, party, account=account, values=values, error=_message(exc)),
            400,
        )
    return _redirect(f"{BASE}/{account_id}")


@router.post(BASE + "/{account_id}/status", response_model=None)
async def customer_status(
    account_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(MANAGE)),
) -> HTMLResponse | RedirectResponse:
    _account_or_404(db, tenant, account_id)
    values = _form(await request.form())
    try:
        service.change_status(
            db,
            tenant=tenant,
            party=party,
            account_id=account_id,
            target=_status(values.get("target") or ""),
            confirm_close=values.get("confirm") == "close",
        )
    except CustomerRefused as exc:
        return _detail_page(request, db, tenant, party, account_id, error=_message(exc))
    return _redirect(f"{BASE}/{account_id}")


customers_web_feature = FeatureManifest(
    name="erp_customers_web",
    web_routers=(router,),
    audit_actions=service.AUDIT_ACTIONS,
    setting_domains=(str(settings.DOMAIN),),
)
