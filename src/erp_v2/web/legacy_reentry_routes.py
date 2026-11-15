"""Legacy never-posted AP re-entry form (admin, like the other supplier drafts)."""

from __future__ import annotations

from uuid import uuid4

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.legacy_ap import service
from erp_v2.web import revenue_queries as queries
from erp_v2.web.revenue_routes import WRITE_PERMISSION

router = APIRouter(tags=["legacy-ap-reentry-web"])


def _page(request: Request, db: Session, tenant: Tenant, party: Party, values: dict, error=None, status_code=200):
    return render(
        request,
        "erp_v2/revenue/draft.html",
        {
            **ui.template_globals(),
            "current_email": party.email,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "accounts": queries.posting_accounts(db, tenant_id=tenant.id),
            "values": values,
            "legacy": True,
            "form_action": "/finance/supplier-invoices/legacy-reentry",
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/finance/supplier-invoices/legacy-reentry")
def legacy_reentry_new(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(WRITE_PERMISSION)),
) -> HTMLResponse:
    if service.active_cohort(db, tenant.id) is None:
        raise HTTPException(status_code=404, detail="Legacy re-entry is not enabled")
    return _page(request, db, tenant, party, {"request_id": str(uuid4())})


@router.post("/finance/supplier-invoices/legacy-reentry", response_model=None)
async def legacy_reentry_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(WRITE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str) and key != "csrf_token"}
    try:
        invoice = service.create_legacy_reentry_draft(db, tenant=tenant, party=party, values=values)
    except service.LegacyReentryRefused as exc:
        return _page(request, db, tenant, party, values, error=str(exc), status_code=400)
    return RedirectResponse(f"/finance/supplier-invoices/{invoice.id}", status_code=303)


legacy_reentry_feature = FeatureManifest(name="erp_legacy_reentry", web_routers=(router,))
