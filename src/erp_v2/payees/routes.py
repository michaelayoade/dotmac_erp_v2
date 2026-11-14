"""Payee bank instruction workspace. Every route is permission-gated and dormant until the
named roles have holders (access_cli). Admin and staff hold none of these permissions.

Account numbers are never rendered except by the audited ``reveal`` action, whose response
is ``no-store``. Responses to refusals carry the stable error code and message only.
"""

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.access import (
    PAYEE_BANK_APPROVE,
    PAYEE_BANK_PROPOSE,
    PAYEE_BANK_READ,
    PAYEE_BANK_READ_UNMASKED,
    PAYEE_BANK_REVOKE,
)
from erp_v2.payees import service

router = APIRouter(tags=["payee-bank-web"])
BASE = "/finance/payee-bank-details"

# route -> permission, asserted against the access profile in tests.
ROUTE_PERMISSIONS = {
    "list": PAYEE_BANK_READ,
    "propose": PAYEE_BANK_PROPOSE,
    "approve": PAYEE_BANK_APPROVE,
    "reject": PAYEE_BANK_APPROVE,
    "revoke": PAYEE_BANK_REVOKE,
    "reveal": PAYEE_BANK_READ_UNMASKED,
}


def _context(request: Request, party: Party, **extra: object) -> dict[str, object]:
    return {
        **ui.template_globals(),
        "current_email": party.email,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        **extra,
    }


def _page(request: Request, db: Session, tenant: Tenant, party: Party, *, error: str | None = None, status_code=200):
    response = render(
        request,
        "payees/home.html",
        _context(
            request,
            party,
            instructions=service.list_instructions(db, tenant_id=tenant.id),
            error=error,
        ),
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def _values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf_token"}


@router.get(BASE)
def payee_bank_list(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ROUTE_PERMISSIONS["list"])),
) -> HTMLResponse:
    return _page(request, db, tenant, party)


@router.post(BASE, response_model=None)
async def payee_bank_propose(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ROUTE_PERMISSIONS["propose"])),
) -> HTMLResponse | RedirectResponse:
    values = await _values(request)
    try:
        service.propose(
            db,
            tenant_id=tenant.id,
            proposer_party_id=party.id,
            payee_kind=values.get("payee_kind", ""),
            payee_ref=values.get("payee_ref", ""),
            bank_code=values.get("bank_code", ""),
            bank_name=values.get("bank_name", ""),
            account_number=values.get("account_number", ""),
            account_name=values.get("account_name", ""),
            evidence_ref=values.get("evidence_ref", ""),
        )
    except service.PayeeBankError as exc:
        return _page(request, db, tenant, party, error=f"{exc.code}: {exc}", status_code=400)
    return RedirectResponse(BASE, status_code=303)


def _decision(action):
    async def handler(
        instruction_id: UUID,
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
                    instruction_id=instruction_id,
                    approver_party_id=party.id,
                    note=values.get("note"),
                )
            elif action == "reject":
                service.reject(
                    db,
                    tenant_id=tenant.id,
                    instruction_id=instruction_id,
                    decider_party_id=party.id,
                    reason=values.get("reason", ""),
                )
            else:
                service.revoke(
                    db,
                    tenant_id=tenant.id,
                    instruction_id=instruction_id,
                    revoker_party_id=party.id,
                    reason=values.get("reason", ""),
                )
        except service.PayeeBankError as exc:
            return _page(request, db, tenant, party, error=f"{exc.code}: {exc}", status_code=400)
        return RedirectResponse(BASE, status_code=303)

    handler.__name__ = f"payee_bank_{action}"
    return handler


for _action in ("approve", "reject", "revoke"):
    router.add_api_route(
        f"{BASE}/{{instruction_id}}/{_action}", _decision(_action), methods=["POST"], response_model=None
    )


@router.post(f"{BASE}/{{instruction_id}}/reveal", response_model=None)
async def payee_bank_reveal(
    instruction_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(ROUTE_PERMISSIONS["reveal"])),
) -> HTMLResponse:
    values = await _values(request)
    try:
        number = service.reveal_account_number(
            db,
            tenant_id=tenant.id,
            instruction_id=instruction_id,
            actor_party_id=party.id,
            reason=values.get("reason", ""),
        )
    except service.PayeeBankError as exc:
        return _page(request, db, tenant, party, error=f"{exc.code}: {exc}", status_code=400)
    # The audit row must be durable before the number is shown (REVIEW-B8 L1).
    db.commit()
    response = render(request, "payees/reveal.html", _context(request, party, account_number=number))
    response.headers["Cache-Control"] = "no-store"
    return response


payee_bank_feature = FeatureManifest(name="erp_payee_bank", web_routers=(router,), audit_actions=service.AUDIT_ACTIONS)
