"""Batch calculate, approve and finalize routes for payroll runs (B4).

Every route is permission-gated: batch calculation needs ``erp.payroll.manage`` (admin, as for
draft creation), approval needs ``erp.payroll.runs.approve`` (payroll_approver) and finalize
needs ``erp.payroll.runs.finalize`` (payroll_finalizer). The two new codes are declared in
``erp_v2.access`` and are dormant until a named party is granted the role (access_cli); admin
and staff hold neither. Refusals render the page again with the reason (HTTP 400); nothing is
echoed from the uploaded input file.
"""

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
from starlette.datastructures import UploadFile

from erp_v2 import ui
from erp_v2.web import payroll_readiness
from erp_v2.web import payroll_run_services as services
from erp_v2.web.payroll_routes import PAYROLL_MANAGE_PERMISSION
from erp_v2.web.payroll_run_input import MAX_FILE_BYTES

APPROVE_PERMISSION: Final = "erp.payroll.runs.approve"
FINALIZE_PERMISSION: Final = "erp.payroll.runs.finalize"
FORM_OVERHEAD_BYTES: Final = 64 * 1024
router = APIRouter(tags=["payroll-run-web"])


def _page(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    run_id: UUID,
    *,
    step: str,
    error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    packet = payroll_readiness.approval_readiness(db, tenant_id=tenant.id, run_id=run_id)
    if packet is None:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    response = render(
        request,
        "payroll/run_step.html",
        {
            **ui.template_globals(),
            "current_email": getattr(party, "email", None),
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "packet": packet,
            "step": step,
            "error": error,
            "notice": notice,
            "max_kib": MAX_FILE_BYTES // 1024,
        },
        status_code=400 if error else 200,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/payroll/runs/{run_id}/batch-calculate")
def payroll_batch_page(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PAYROLL_MANAGE_PERMISSION)),
) -> HTMLResponse:
    return _page(request, db, tenant, party, run_id, step="batch")


@router.post("/payroll/runs/{run_id}/batch-calculate", response_model=None)
async def payroll_batch_calculate(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PAYROLL_MANAGE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_FILE_BYTES + FORM_OVERHEAD_BYTES):
        raise HTTPException(413, "Upload is too large")
    form = await request.form()
    upload = form.get("input_file")
    data: bytes | None = None
    if isinstance(upload, UploadFile) and (upload.filename or ""):
        data = await upload.read(MAX_FILE_BYTES + 1)
    try:
        outcome = services.batch_calculate(db, tenant=tenant, party=party, run_id=run_id, input_bytes=data)
    except services.RunRefused as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail="Payroll run not found") from exc
        return _page(request, db, tenant, party, run_id, step="batch", error=str(exc))
    return _page(
        request,
        db,
        tenant,
        party,
        run_id,
        step="batch",
        notice=f"Calculated {outcome.calculated} employee(s); {outcome.skipped_existing} already calculated.",
    )


@router.get("/payroll/runs/{run_id}/approve")
def payroll_approve_page(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE_PERMISSION)),
) -> HTMLResponse:
    return _page(request, db, tenant, party, run_id, step="approve")


@router.post("/payroll/runs/{run_id}/approve", response_model=None)
def payroll_approve(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    try:
        services.approve(db, tenant=tenant, party=party, run_id=run_id)
    except services.RunRefused as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail="Payroll run not found") from exc
        return _page(request, db, tenant, party, run_id, step="approve", error=str(exc))
    return RedirectResponse(f"/payroll/runs/{run_id}/finalize", status_code=303)


@router.get("/payroll/runs/{run_id}/finalize")
def payroll_finalize_page(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(FINALIZE_PERMISSION)),
) -> HTMLResponse:
    return _page(request, db, tenant, party, run_id, step="finalize")


@router.post("/payroll/runs/{run_id}/finalize", response_model=None)
def payroll_finalize(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(FINALIZE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    try:
        services.finalize(db, tenant=tenant, party=party, run_id=run_id)
    except services.RunRefused as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail="Payroll run not found") from exc
        return _page(request, db, tenant, party, run_id, step="finalize", error=str(exc))
    return RedirectResponse(f"/payroll/runs/{run_id}/finalize", status_code=303)


payroll_run_feature = FeatureManifest(
    name="erp_payroll_run",
    web_routers=(router,),
    audit_actions=services.AUDIT_ACTIONS,
)
