"""ERP web routes: login form/submit, logout, and the portal home.

The facet (declared in assembly.py) mounts this router at `/`.
Login routes are pre-auth by definition; home requires the
`erp.web.access` permission through the kernel cookie guard, which
redirects unauthenticated browsers to the facet login route.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.permissions import PermissionSpec
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import is_secure_request, require_web_permission, safe_next_url
from dotmac_kernel.web_surfaces import BrowserSessionPolicy, WebSurfaceError, current_session_policy
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.web import auth_service

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


erp_web_feature: Final = FeatureManifest(
    name="erp_web",
    web_routers=(router,),
    permissions=(
        PermissionSpec(
            code=ACCESS_PERMISSION,
            description="Enter the ERP web portal.",
            default_roles=("admin", "staff"),
        ),
    ),
)

__all__ = ["ACCESS_PERMISSION", "erp_web_feature", "router"]
