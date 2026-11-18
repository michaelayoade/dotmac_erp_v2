"""Supplier credit note routes (B1c). Drafting and submitting reuse the supplier draft permission;
approve-and-post reuses the B1 approve permission. No new permission code, so no profile bump."""

from __future__ import annotations

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
from erp_v2.web import credit_note_services as services
from erp_v2.web import revenue_queries as queries
from erp_v2.web.revenue_approval_routes import APPROVE_PERMISSION
from erp_v2.web.revenue_routes import ACCESS_PERMISSION, WRITE_PERMISSION
from erp_v2.web.revenue_services import RevenueActionError

router = APIRouter(tags=["supplier-credit-note-web"])


def _context(request: Request, party: Party, **extra: object) -> dict[str, object]:
    return {
        **ui.template_globals(),
        "current_email": party.email,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        **extra,
    }


def _draft_context(request: Request, db: Session, tenant: Tenant, party: Party, **extra: object) -> dict[str, object]:
    return _context(
        request,
        party,
        accounts=queries.posting_accounts(db, tenant_id=tenant.id),
        values={"request_id": str(uuid4())},
        **extra,
    )


def _detail_page(request, db, tenant, party, credit_note_id, *, error=None, status_code=200):
    detail = queries.credit_note_detail(db, tenant_id=tenant.id, credit_note_id=credit_note_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Supplier credit note was not found")
    return render(
        request,
        "erp_v2/revenue/credit_note_detail.html",
        _context(request, party, **detail, error=error),
        status_code=status_code,
    )


@router.get("/finance/supplier-credit-notes/new")
def credit_note_new(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(WRITE_PERMISSION)),
) -> HTMLResponse:
    return render(request, "erp_v2/revenue/credit_note_draft.html", _draft_context(request, db, tenant, party))


@router.post("/finance/supplier-credit-notes", response_model=None)
async def credit_note_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(WRITE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str) and key != "csrf_token"}
    try:
        credit = services.create_credit_note_draft(db, tenant=tenant, party=party, values=values)
    except RevenueActionError as exc:
        context = _draft_context(request, db, tenant, party, error=str(exc))
        context["values"] = values
        return render(request, "erp_v2/revenue/credit_note_draft.html", context, status_code=400)
    return RedirectResponse(f"/finance/supplier-credit-notes/{credit.id}", status_code=303)


@router.get("/finance/supplier-credit-notes/{credit_note_id}")
def credit_note_detail(
    credit_note_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ACCESS_PERMISSION)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, credit_note_id)


@router.post("/finance/supplier-credit-notes/{credit_note_id}/submit", response_model=None)
def credit_note_submit(
    credit_note_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(WRITE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    try:
        services.submit_credit_note_draft(db, tenant=tenant, party=party, credit_note_id=credit_note_id)
    except RevenueActionError as exc:
        return _detail_page(request, db, tenant, party, credit_note_id, error=str(exc), status_code=400)
    return RedirectResponse(f"/finance/supplier-credit-notes/{credit_note_id}", status_code=303)


@router.post("/finance/supplier-credit-notes/{credit_note_id}/approve", response_model=None)
async def credit_note_approve(
    credit_note_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    text = {key: value for key, value in form.items() if isinstance(value, str)}
    obligation_text = text.get("obligation_id", "").strip()
    try:
        obligation_id = UUID(obligation_text) if obligation_text else None
    except ValueError:
        return _detail_page(
            request, db, tenant, party, credit_note_id, error="The obligation reference is not valid", status_code=400
        )
    try:
        services.approve_and_post(
            db,
            tenant=tenant,
            party=party,
            credit_note_id=credit_note_id,
            evidence_ref=text.get("evidence_ref"),
            obligation_id=obligation_id,
            apply_amount=text.get("apply_amount", "").strip() or None,
        )
    except services.CreditApprovalRefused as exc:
        return _detail_page(request, db, tenant, party, credit_note_id, error=str(exc), status_code=400)
    return RedirectResponse(f"/finance/supplier-credit-notes/{credit_note_id}", status_code=303)


credit_note_feature = FeatureManifest(
    name="erp_supplier_credit_notes",
    web_routers=(router,),
    audit_actions=(services.AUDIT_APPROVED_POSTED, services.AUDIT_APPROVAL_REFUSED),
)
