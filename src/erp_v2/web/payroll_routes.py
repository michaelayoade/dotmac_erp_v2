"""Permission-guarded draft payroll forms; mounted by the ERP web facet."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.permissions import PermissionSpec
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from dotmac_payroll.models import EmployeePayAssignment, PayrollCalculation, PayrollRun
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.web import payroll_actions, payroll_readiness

PAYROLL_MANAGE_PERMISSION: Final = "erp.payroll.manage"
router = APIRouter(tags=["payroll"])


def _page(request: Request, db: Session, tenant: Tenant, party: Party, error: str | None = None) -> HTMLResponse:
    return render(
        request,
        "payroll/actions.html",
        {
            **ui.template_globals(),
            "current_email": getattr(party, "email", None),
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "error": error,
            "runs": db.scalars(
                select(PayrollRun)
                .where(PayrollRun.tenant_id == tenant.id)
                .order_by(PayrollRun.period_start.desc())
                .limit(50)
            ).all(),
            "assignments": db.scalars(
                select(EmployeePayAssignment)
                .where(EmployeePayAssignment.tenant_id == tenant.id, EmployeePayAssignment.status == "active")
                .order_by(EmployeePayAssignment.employee_ref)
                .limit(200)
            ).all(),
            "calculations": db.execute(
                select(PayrollCalculation, PayrollRun.run_ref, PayrollRun.currency_code)
                .join(
                    PayrollRun,
                    (PayrollRun.tenant_id == PayrollCalculation.tenant_id)
                    & (PayrollRun.id == PayrollCalculation.run_id),
                )
                .where(PayrollCalculation.tenant_id == tenant.id)
                .order_by(PayrollCalculation.calculated_at.desc())
                .limit(20)
            ).all(),
        },
    )


@router.get("/payroll/actions")
def payroll_actions_page(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PAYROLL_MANAGE_PERMISSION)),
) -> HTMLResponse:
    return _page(request, db, tenant, party)


@router.post("/payroll/runs", response_model=None)
async def payroll_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PAYROLL_MANAGE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    try:
        payroll_actions.create_draft(
            db,
            tenant=tenant,
            party=party,
            **{
                name: str(form.get(name, ""))
                for name in ("run_ref", "period_start", "period_end", "currency_code", "minor_units")
            },
        )
    except payroll_actions.PayrollActionError as exc:
        return _page(request, db, tenant, party, str(exc))
    return RedirectResponse("/payroll/actions", status_code=303)


@router.get("/payroll/runs/{run_id}/approval-readiness")
def payroll_approval_readiness(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PAYROLL_MANAGE_PERMISSION)),
) -> HTMLResponse:
    packet = payroll_readiness.approval_readiness(db, tenant_id=tenant.id, run_id=run_id)
    if packet is None:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    response = render(
        request,
        "payroll/approval_readiness.html",
        {**ui.template_globals(), "current_email": getattr(party, "email", None), "packet": packet},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/payroll/runs/{run_id}/calculate", response_model=None)
async def payroll_calculate(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PAYROLL_MANAGE_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    try:
        payroll_actions.calculate_employee(
            db,
            tenant=tenant,
            run_id=run_id,
            **{name: str(form.get(name, "")) for name in ("employee_ref", "proration_factor", "component_inputs")},
        )
    except payroll_actions.PayrollActionError as exc:
        return _page(request, db, tenant, party, str(exc))
    return RedirectResponse("/payroll/actions", status_code=303)


payroll_web_feature = FeatureManifest(
    name="erp_payroll",
    web_routers=(router,),
    permissions=(
        PermissionSpec(
            code=PAYROLL_MANAGE_PERMISSION,
            description="Create payroll drafts and calculate employee payroll.",
            default_roles=("admin",),
        ),
    ),
)
