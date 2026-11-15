"""Bank reconciliation pages (B7). Browser routes, permission-gated.

Dormant until a named party holds ``erp.banking.reconciliation.prepare`` (bank_reconciler: open,
match, unmatch, itemize, prepare, cancel) or ``erp.banking.reconciliation.approve`` (finance_approver:
approve or return). admin and staff hold neither (W14), and the holder must also be staff because
these are not portal entry routes (access_cli enforces it). Every POST carries the kernel CSRF token.

Nothing here posts to the ledger: unrecorded bank charges are shown as typed items that block
preparation until a B2 manual journal records them.
"""

from __future__ import annotations

from datetime import date
from typing import Final
from uuid import UUID

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.banking import recon_models as m
from erp_v2.banking import recon_queries as queries
from erp_v2.banking import recon_service as svc
from erp_v2.banking.recon_service import ItemRequest, ReconRefused

PREPARE: Final = "erp.banking.reconciliation.prepare"
APPROVE: Final = "erp.banking.reconciliation.approve"
BASE: Final = "/finance/banking/reconciliations"
APPROVALS: Final = "/finance/banking/reconciliation-approvals"
router = APIRouter(tags=["banking-reconciliation"])


def _ctx(request: Request, party: Party, **extra: object) -> dict[str, object]:
    return {
        **ui.template_globals(),
        "current_email": party.email,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        **extra,
    }


def _page(request: Request, template: str, context: dict[str, object], status_code: int = 200) -> HTMLResponse:
    response = render(request, template, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def _form(request: Request) -> dict[str, list[str]]:
    form = await request.form()
    out: dict[str, list[str]] = {}
    for key in form:
        if key == "csrf_token":
            continue
        out[key] = [v for v in form.getlist(key) if isinstance(v, str)]
    return out


def _one(values: dict[str, list[str]], key: str) -> str:
    found = values.get(key) or [""]
    return found[0]


def _uuids(values: dict[str, list[str]], key: str) -> list[UUID]:
    out: list[UUID] = []
    for raw in values.get(key, []):
        try:
            out.append(UUID(raw))
        except ValueError:
            raise ReconRefused("invalid_input", "a selected line identifier is not valid") from None
    return out


def _workspace(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    reconciliation_id: UUID,
    *,
    approver: bool,
    bank_page: int = 1,
    gl_page: int = 1,
    error: str | None = None,
) -> HTMLResponse:
    data = queries.workspace(
        db, tenant_id=tenant.id, reconciliation_id=reconciliation_id, bank_page=bank_page, gl_page=gl_page
    )
    if data is None:
        raise HTTPException(status_code=404, detail="Reconciliation was not found")
    return _page(
        request,
        "banking/recon_detail.html",
        _ctx(request, party, **data, approver=approver, error=error, base=APPROVALS if approver else BASE),
        status_code=400 if error else 200,
    )


def _refusal(exc: ReconRefused) -> str:
    return exc.detail or exc.code


# --- preparer (bank_reconciler) --------------------------------------------------


@router.get(BASE)
def recon_register(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _page(
        request,
        "banking/recon_register.html",
        _ctx(
            request,
            party,
            rows=queries.reconciliations(db, tenant_id=tenant.id),
            openable=queries.openable_statements(db, tenant_id=tenant.id),
        ),
    )


@router.post(BASE, response_model=None)
async def recon_open(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    values = await _form(request)
    try:
        statement_id = UUID(_one(values, "statement_id"))
        account_id = queries.statement_account(db, tenant_id=tenant.id, statement_id=statement_id)
        if account_id is None:
            raise ReconRefused("statement_unknown", "statement not found in this tenant")
        raw_date = _one(values, "baseline_date").strip()
        row = svc.open_reconciliation(
            db,
            tenant_id=tenant.id,
            actor=party.id,
            account_id=account_id,
            statement_id=statement_id,
            baseline_date=date.fromisoformat(raw_date) if raw_date else None,
        )
    except ValueError as exc:
        error = _refusal(exc) if isinstance(exc, ReconRefused) else "Choose a statement"
        return _page(
            request,
            "banking/recon_register.html",
            _ctx(
                request,
                party,
                rows=queries.reconciliations(db, tenant_id=tenant.id),
                openable=queries.openable_statements(db, tenant_id=tenant.id),
                error=error,
            ),
            status_code=400,
        )
    return _redirect(f"{BASE}/{row.id}")


@router.get(BASE + "/{reconciliation_id}")
def recon_detail(
    reconciliation_id: UUID,
    request: Request,
    bank_page: int = Query(1, ge=1, le=svc.MAX_PAGE),
    gl_page: int = Query(1, ge=1, le=svc.MAX_PAGE),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _workspace(
        request, db, tenant, party, reconciliation_id, approver=False, bank_page=bank_page, gl_page=gl_page
    )


async def _act(request, db, tenant, party, reconciliation_id, action, *, approver=False):
    values = await _form(request)
    try:
        action(values)
    except ReconRefused as exc:
        return _workspace(request, db, tenant, party, reconciliation_id, approver=approver, error=_refusal(exc))
    return _redirect(f"{APPROVALS if approver else BASE}/{reconciliation_id}")


@router.post(BASE + "/{reconciliation_id}/match", response_model=None)
async def recon_match(
    reconciliation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    def action(values: dict[str, list[str]]) -> None:
        account_id = svc.live_reconciliation_account(db, tenant.id, reconciliation_id)
        svc.match(
            db,
            tenant_id=tenant.id,
            actor=party.id,
            account_id=account_id,
            bank_line_ids=_uuids(values, "bank_line"),
            gl_line_ids=_uuids(values, "gl_line"),
        )

    return await _act(request, db, tenant, party, reconciliation_id, action)


@router.post(BASE + "/{reconciliation_id}/unmatch", response_model=None)
async def recon_unmatch(
    reconciliation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    def action(values: dict[str, list[str]]) -> None:
        svc.live_reconciliation_account(db, tenant.id, reconciliation_id)
        ids = _uuids(values, "match_id")
        if len(ids) != 1:
            raise ReconRefused("invalid_input", "choose exactly one match")
        svc.unmatch(db, tenant_id=tenant.id, actor=party.id, match_id=ids[0], reason=_one(values, "reason"))

    return await _act(request, db, tenant, party, reconciliation_id, action)


@router.post(BASE + "/{reconciliation_id}/items", response_model=None)
async def recon_add_items(
    reconciliation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    def action(values: dict[str, list[str]]) -> None:
        note = _one(values, "note")
        raw_journal = _one(values, "journal_id").strip()
        journal_id = UUID(raw_journal) if raw_journal else None
        requests = [
            ItemRequest(m.SIDE_GL, ref, _one(values, "gl_item_type"), note) for ref in _uuids(values, "gl_line")
        ] + [
            ItemRequest(m.SIDE_BANK, ref, _one(values, "bank_item_type"), note, journal_id)
            for ref in _uuids(values, "bank_line")
        ]
        svc.add_items(db, tenant_id=tenant.id, actor=party.id, reconciliation_id=reconciliation_id, requests=requests)

    return await _act(request, db, tenant, party, reconciliation_id, action)


@router.post(BASE + "/{reconciliation_id}/items/{item_id}/withdraw", response_model=None)
async def recon_withdraw_item(
    reconciliation_id: UUID,
    item_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    def action(values: dict[str, list[str]]) -> None:
        svc.withdraw_item(
            db,
            tenant_id=tenant.id,
            actor=party.id,
            reconciliation_id=reconciliation_id,
            item_id=item_id,
            reason=_one(values, "reason"),
        )

    return await _act(request, db, tenant, party, reconciliation_id, action)


@router.post(BASE + "/{reconciliation_id}/prepare", response_model=None)
async def recon_prepare(
    reconciliation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    def action(_values: dict[str, list[str]]) -> None:
        svc.prepare(db, tenant_id=tenant.id, actor=party.id, reconciliation_id=reconciliation_id)

    return await _act(request, db, tenant, party, reconciliation_id, action)


@router.post(BASE + "/{reconciliation_id}/cancel", response_model=None)
async def recon_cancel(
    reconciliation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    def action(values: dict[str, list[str]]) -> None:
        svc.cancel(
            db, tenant_id=tenant.id, actor=party.id, reconciliation_id=reconciliation_id, reason=_one(values, "reason")
        )

    return await _act(request, db, tenant, party, reconciliation_id, action)


# --- approver (finance_approver) -------------------------------------------------------


@router.get(APPROVALS)
def approvals_queue(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _page(
        request,
        "banking/recon_approvals.html",
        _ctx(request, party, rows=queries.reconciliations(db, tenant_id=tenant.id, statuses=(m.STATUS_PREPARED,))),
    )


@router.get(APPROVALS + "/{reconciliation_id}")
def approval_detail(
    reconciliation_id: UUID,
    request: Request,
    bank_page: int = Query(1, ge=1, le=svc.MAX_PAGE),
    gl_page: int = Query(1, ge=1, le=svc.MAX_PAGE),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _workspace(
        request, db, tenant, party, reconciliation_id, approver=True, bank_page=bank_page, gl_page=gl_page
    )


@router.post(APPROVALS + "/{reconciliation_id}/approve", response_model=None)
async def approval_approve(
    reconciliation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse | RedirectResponse:
    def action(_values: dict[str, list[str]]) -> None:
        svc.approve(db, tenant_id=tenant.id, actor=party.id, reconciliation_id=reconciliation_id)

    return await _act(request, db, tenant, party, reconciliation_id, action, approver=True)


@router.post(APPROVALS + "/{reconciliation_id}/return", response_model=None)
async def approval_return(
    reconciliation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse | RedirectResponse:
    def action(values: dict[str, list[str]]) -> None:
        svc.return_to_open(
            db, tenant_id=tenant.id, actor=party.id, reconciliation_id=reconciliation_id, reason=_one(values, "reason")
        )

    return await _act(request, db, tenant, party, reconciliation_id, action, approver=True)


banking_recon_feature = FeatureManifest(
    name="erp_banking_reconciliation", web_routers=(router,), audit_actions=svc.AUDIT_ACTIONS
)
