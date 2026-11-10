"""Guarded workspace only; observation HTTP adapter awaits trusted auth integration."""

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.banking import queries

router = APIRouter(tags=["banking-web"])


@router.get("/finance/banking")
def banking_workspace(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission("erp.web.access")),
) -> HTMLResponse:
    return render(
        request,
        "banking/home.html",
        {
            **ui.template_globals(),
            "current_email": party.email,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            **queries.workspace(db, tenant_id=tenant.id),
        },
    )


banking_web_feature = FeatureManifest(name="erp_banking", web_routers=(router,))
