"""ERP web routes: login form/submit, logout, and the portal home.

The facet (declared in assembly.py) mounts this router at `/`.
Login routes are pre-auth by definition; home requires the
`erp.web.access` permission through the kernel cookie guard, which
redirects unauthenticated browsers to the facet login route.
"""

from __future__ import annotations

from datetime import date
from typing import Final
from urllib.parse import quote
from uuid import UUID

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest, NavItem
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.permissions import PermissionSpec
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import is_secure_request, require_web_permission, safe_next_url
from dotmac_kernel.web_surfaces import BrowserSessionPolicy, WebSurfaceError, current_session_policy
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.web import (
    auth_service,
    finance_queries,
    inventory_queries,
    payroll_queries,
    payroll_routes,
    people_actions,
    people_queries,
)

router = APIRouter(tags=["web"])

ACCESS_PERMISSION: Final = "erp.web.access"
_COOKIE_FALLBACK = "access_token"


def _session_policy(request: Request) -> BrowserSessionPolicy:
    try:
        return current_session_policy(request)
    except WebSurfaceError:
        return BrowserSessionPolicy(cookie_name=_COOKIE_FALLBACK)


def _context(**extra: object) -> dict[str, object]:
    return {**ui.template_globals(), **extra}


@router.get("/login")
def login_page(request: Request, next: str | None = None, tenant: Tenant = Depends(require_tenant)) -> HTMLResponse:
    return render(
        request,
        "auth/login.html",
        _context(next_url=safe_next_url(next, default="/"), error=None),
    )


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))
    next_url = safe_next_url(str(form.get("next", "")), default="/")
    if not email or not password:
        return render(
            request,
            "auth/login.html",
            _context(next_url=next_url, error="Email and password are required"),
        )
    token = auth_service.web_login(db, tenant, email, password)
    if token is None:
        return render(
            request,
            "auth/login.html",
            _context(next_url=next_url, error="Invalid email or password"),
        )
    response = RedirectResponse(url=quote(next_url, safe="/?=&"), status_code=302)
    policy = _session_policy(request)
    response.set_cookie(
        policy.cookie_name,
        token,
        httponly=True,
        samesite="lax",
        secure=is_secure_request(request),
        path=policy.cookie_path or "/",
    )
    return response


@router.post("/logout")
def logout(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> RedirectResponse:
    policy = _session_policy(request)
    auth_service.web_logout(db, tenant, request.cookies.get(policy.cookie_name))
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(policy.cookie_name, path=policy.cookie_path or "/")
    return response


@router.get("/people")
def people_home(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    return render(
        request,
        "people/employees.html",
        _context(
            current_email=getattr(party, "email", None),
            employees=people_queries.list_employees(db, tenant_id=tenant.id),
        ),
    )


def _leave_context(request: Request, db: Session, tenant: Tenant, party: Party, **extra: object) -> dict[str, object]:
    overview = people_queries.leave_overview(db, tenant_id=tenant.id)
    return _context(
        current_email=getattr(party, "email", None),
        leave_types=people_queries.list_leave_types(db, tenant_id=tenant.id),
        csrf_token=getattr(request.state, "csrf_token", ""),
        **overview,
        **extra,
    )


@router.get("/people/leave")
def leave_home(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    return render(request, "people/leave.html", _leave_context(request, db, tenant, party))


@router.post("/people/leave/apply", response_model=None)
async def leave_apply(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    try:
        from_date = date.fromisoformat(str(form.get("from_date", "")))
        to_date = date.fromisoformat(str(form.get("to_date", "")))
    except ValueError:
        return render(
            request,
            "people/leave.html",
            _leave_context(request, db, tenant, party, error="Enter both dates (YYYY-MM-DD)"),
        )
    try:
        people_actions.apply_for_leave(
            db,
            tenant=tenant,
            party=party,
            leave_type_code=str(form.get("leave_type_code", "")),
            from_date=from_date,
            to_date=to_date,
            allocate_number=people_actions.numbering_allocator(db, tenant),
            half_day=form.get("half_day") == "on",
            reason=str(form.get("reason", "")) or None,
        )
    except people_actions.PeopleActionError as exc:
        return render(
            request,
            "people/leave.html",
            _leave_context(request, db, tenant, party, error=str(exc)),
        )
    return RedirectResponse(url="/people/leave", status_code=303)


@router.post("/people/leave/{application_id}/decide", response_model=None)
async def leave_decide(
    application_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    try:
        people_actions.decide_leave(
            db,
            tenant=tenant,
            party=party,
            application_id=application_id,
            approve=form.get("decision") == "approve",
            rejection_reason=str(form.get("rejection_reason", "")) or None,
        )
    except people_actions.PeopleActionError as exc:
        return render(
            request,
            "people/leave.html",
            _leave_context(request, db, tenant, party, error=str(exc)),
        )
    return RedirectResponse(url="/people/leave", status_code=303)


@router.post("/people/leave/{application_id}/cancel", response_model=None)
def leave_cancel(
    application_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    try:
        people_actions.cancel_leave(db, tenant=tenant, party=party, application_id=application_id)
    except people_actions.PeopleActionError as exc:
        return render(
            request,
            "people/leave.html",
            _leave_context(request, db, tenant, party, error=str(exc)),
        )
    return RedirectResponse(url="/people/leave", status_code=303)


@router.get("/finance")
def finance_home(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    balance = finance_queries.trial_balance(db, tenant_id=tenant.id)
    return render(
        request,
        "finance/home.html",
        _context(
            current_email=getattr(party, "email", None),
            accounts=finance_queries.chart_of_accounts(db, tenant_id=tenant.id),
            journals=finance_queries.recent_journals(db, tenant_id=tenant.id, limit=10),
            **balance,
        ),
    )


@router.get("/finance/payables")
def payables_home(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    return render(
        request,
        "finance/payables.html",
        _context(
            current_email=getattr(party, "email", None),
            invoices=finance_queries.open_payables(db, tenant_id=tenant.id),
        ),
    )


@router.get("/payroll")
def payroll_home(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    return render(
        request,
        "payroll/home.html",
        _context(
            current_email=getattr(party, "email", None),
            runs=payroll_queries.payroll_runs(db, tenant_id=tenant.id),
            structures=payroll_queries.pay_structures(db, tenant_id=tenant.id),
            components=payroll_queries.pay_components(db, tenant_id=tenant.id),
            assignments_active=payroll_queries.assignment_count(db, tenant_id=tenant.id),
        ),
    )


@router.get("/inventory")
def inventory_home(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    overview = inventory_queries.stock_overview(db, tenant_id=tenant.id)
    return render(
        request,
        "inventory/home.html",
        _context(
            current_email=getattr(party, "email", None),
            movements=inventory_queries.recent_movements(db, tenant_id=tenant.id),
            **overview,
        ),
    )


@router.get("/")
def home(
    request: Request,
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    return render(
        request,
        "home.html",
        _context(current_email=getattr(party, "email", None)),
    )


router.include_router(payroll_routes.router)


erp_web_feature: Final = FeatureManifest(
    name="erp_web",
    web_routers=(router,),
    nav=[
        NavItem("People", "/people"),
        NavItem("Finance", "/finance"),
        NavItem("Payroll", "/payroll"),
        NavItem("Inventory", "/inventory"),
    ],
    permissions=(
        PermissionSpec(
            code=payroll_routes.PAYROLL_MANAGE_PERMISSION,
            description="Create payroll drafts and calculate employee payroll.",
            default_roles=("admin",),
        ),
        PermissionSpec(
            code=ACCESS_PERMISSION,
            description="Enter the ERP web portal.",
            default_roles=("admin", "staff"),
        ),
    ),
)

__all__ = ["ACCESS_PERMISSION", "erp_web_feature", "router"]
