"""Browser supplier entry and finance evidence over released owner contracts."""

from __future__ import annotations

from uuid import UUID, uuid4

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.permissions import PermissionSpec
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.web import revenue_queries as queries
from erp_v2.web import revenue_services as services

ACCESS_PERMISSION = "erp.web.access"
WRITE_PERMISSION = "erp.finance.payables.write"
router = APIRouter(tags=["revenue-web"])


def _context(request: Request, party: Party, **extra: object) -> dict[str, object]:
    return {
        **ui.template_globals(),
        "current_email": party.email,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        **extra,
    }


@router.get("/finance/receivables")
def receivables_register(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    return render(
        request,
        "erp_v2/revenue/receivables.html",
        _context(request, party, **queries.billing_register(db, tenant_id=tenant.id)),
    )


@router.get("/finance/supplier-invoices")
def supplier_invoices(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    return render(
        request,
        "erp_v2/revenue/suppliers.html",
        _context(request, party, invoices=queries.supplier_register(db, tenant_id=tenant.id)),
    )


def _draft_context(request: Request, db: Session, tenant: Tenant, party: Party, **extra: object) -> dict[str, object]:
    return _context(
        request,
        party,
        accounts=queries.posting_accounts(db, tenant_id=tenant.id),
        values={"request_id": str(uuid4())},
        **extra,
    )


@router.get("/finance/supplier-invoices/new")
def supplier_invoice_new(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(WRITE_PERMISSION)),
) -> HTMLResponse:
    return render(request, "erp_v2/revenue/draft.html", _draft_context(request, db, tenant, party))


@router.post("/finance/supplier-invoices", response_model=None)
async def supplier_invoice_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(WRITE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str) and key != "csrf_token"}
    try:
        invoice = services.create_supplier_draft(db, tenant=tenant, party=party, values=values)
    except services.RevenueActionError as exc:
        context = _draft_context(request, db, tenant, party, error=str(exc))
        context["values"] = values
        return render(request, "erp_v2/revenue/draft.html", context, status_code=400)
    return RedirectResponse(f"/finance/supplier-invoices/{invoice.id}", status_code=303)


@router.get("/finance/supplier-invoices/{invoice_id}")
def supplier_invoice_detail(
    invoice_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    detail = queries.supplier_detail(db, tenant_id=tenant.id, invoice_id=invoice_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Supplier invoice was not found")
    return render(request, "erp_v2/revenue/supplier_detail.html", _context(request, party, **detail))


@router.post("/finance/supplier-invoices/{invoice_id}/submit", response_model=None)
def supplier_invoice_submit(
    invoice_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(WRITE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    try:
        services.submit_supplier_draft(db, tenant=tenant, party=party, invoice_id=invoice_id)
    except services.RevenueActionError as exc:
        detail = queries.supplier_detail(db, tenant_id=tenant.id, invoice_id=invoice_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Supplier invoice was not found") from exc
        return render(
            request,
            "erp_v2/revenue/supplier_detail.html",
            _context(request, party, **detail, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(f"/finance/supplier-invoices/{invoice_id}", status_code=303)


revenue_web_feature = FeatureManifest(
    name="erp_revenue",
    web_routers=(router,),
    permissions=(
        PermissionSpec(
            code=WRITE_PERMISSION,
            description="Create and submit supplier invoice drafts for review.",
            default_roles=("admin",),
        ),
    ),
)
