"""Approve-and-post route for submitted supplier invoices (B1). Dormant until a
named party holds finance_approver; admin and staff hold no approve permission."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.web import revenue_approval_services as services
from erp_v2.web import revenue_queries as queries

APPROVE_PERMISSION: Final = "erp.finance.supplier_invoices.approve"
router = APIRouter(tags=["revenue-approval-web"])


@router.post("/finance/supplier-invoices/{invoice_id}/approve", response_model=None)
def supplier_invoice_approve(
    invoice_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    try:
        services.approve_and_post(db, tenant=tenant, party=party, invoice_id=invoice_id)
    except services.ApprovalRefused as exc:
        detail = queries.supplier_detail(db, tenant_id=tenant.id, invoice_id=invoice_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Supplier invoice was not found") from exc
        return render(
            request,
            "erp_v2/revenue/supplier_detail.html",
            {
                **ui.template_globals(),
                "current_email": party.email,
                "csrf_token": getattr(request.state, "csrf_token", ""),
                **detail,
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(f"/finance/supplier-invoices/{invoice_id}", status_code=303)


revenue_approval_feature = FeatureManifest(
    name="erp_revenue_approval",
    web_routers=(router,),
    audit_actions=(services.AUDIT_APPROVED_POSTED, services.AUDIT_APPROVAL_REFUSED),
)
