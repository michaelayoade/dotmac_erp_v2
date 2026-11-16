"""Cash GL re-point workspace. Dormant until named parties hold the roles (access_cli).

``finance_preparer`` proposes, ``finance_approver`` approves or rejects; admin and staff hold
neither permission. Nothing here posts to the ledger: Finance moves balances with B2 journals.
"""

from __future__ import annotations

from uuid import UUID

from dotmac_accounting.models import Account
from dotmac_banking.models import BankAccount
from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.access import CASH_GL_REPOINT_APPROVE, CASH_GL_REPOINT_PROPOSE, CASH_GL_REPOINT_READ
from erp_v2.cash_gl import service

router = APIRouter(tags=["cash-gl-repoint-web"])
BASE = "/finance/banking/cash-gl-repoints"

# route -> permission, asserted against the access profile in tests.
ROUTE_PERMISSIONS = {
    "list": CASH_GL_REPOINT_READ,
    "propose": CASH_GL_REPOINT_PROPOSE,
    "approve": CASH_GL_REPOINT_APPROVE,
    "reject": CASH_GL_REPOINT_APPROVE,
}


def _page(request: Request, db: Session, tenant: Tenant, party: Party, *, error: str | None = None, status_code=200):
    banks = list(
        db.scalars(select(BankAccount).where(BankAccount.tenant_id == tenant.id).order_by(BankAccount.account_code))
    )
    response = render(
        request,
        "cash_gl/home.html",
        {
            **ui.template_globals(),
            "current_email": party.email,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "banks": banks,
            "requests": service.list_requests(db, tenant_id=tenant.id),
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def _values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf_token"}


@router.get(BASE)
def cash_gl_list(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ROUTE_PERMISSIONS["list"])),
) -> HTMLResponse:
    return _page(request, db, tenant, party)


@router.post(BASE, response_model=None)
async def cash_gl_propose(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ROUTE_PERMISSIONS["propose"])),
) -> HTMLResponse | RedirectResponse:
    values = await _values(request)
    try:
        try:
            bank_account_id = UUID(values.get("bank_account_id", ""))
        except ValueError as exc:
            raise service.RepointRefused("invalid_input", "select a bank account") from exc
        code = (values.get("target_code") or "").strip()
        target = db.scalar(select(Account.id).where(Account.tenant_id == tenant.id, Account.code == code))
        if target is None:
            raise service.RepointRefused("target_unknown", "no account has that code in this tenant")
        service.propose(
            db,
            tenant_id=tenant.id,
            proposer_party_id=party.id,
            bank_account_id=bank_account_id,
            target_account_id=target,
            evidence_ref=values.get("evidence_ref", ""),
        )
    except service.RepointRefused as exc:
        db.rollback()
        return _page(request, db, tenant, party, error=f"{exc.code}: {exc}", status_code=400)
    return RedirectResponse(BASE, status_code=303)


def _decision(action: str):
    async def handler(
        request_id: UUID,
        request: Request,
        db: Session = Depends(get_db),
        tenant: Tenant = Depends(require_tenant),
        party: Party = Depends(require_web_permission(ROUTE_PERMISSIONS[action])),
    ) -> HTMLResponse | RedirectResponse:
        values = await _values(request)
        try:
            if action == "approve":
                service.approve(
                    db,
                    tenant_id=tenant.id,
                    request_id=request_id,
                    approver_party_id=party.id,
                    note=values.get("note"),
                )
            else:
                service.reject(
                    db,
                    tenant_id=tenant.id,
                    request_id=request_id,
                    decider_party_id=party.id,
                    reason=values.get("reason", ""),
                )
        except service.RepointRefused as exc:
            db.rollback()
            return _page(request, db, tenant, party, error=f"{exc.code}: {exc}", status_code=400)
        return RedirectResponse(BASE, status_code=303)

    handler.__name__ = f"cash_gl_{action}"
    return handler


for _action in ("approve", "reject"):
    router.add_api_route(f"{BASE}/{{request_id}}/{_action}", _decision(_action), methods=["POST"], response_model=None)

cash_gl_feature = FeatureManifest(name="erp_cash_gl", web_routers=(router,), audit_actions=service.AUDIT_ACTIONS)
