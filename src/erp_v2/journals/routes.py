"""Web routes for manual journals, held legacy journals and fiscal periods.

Every route is permission-gated by the P0 codes and dormant until a named party holds
the role (access_cli). admin and staff hold none of them (W14). There is deliberately
no route that acts on more than one journal: held legacy journals are reviewed and posted
one at a time (W13).
"""

from __future__ import annotations

from datetime import date
from typing import Final
from uuid import UUID, uuid4

from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.journals import forms, policy, queries, service
from erp_v2.journals.service import JournalRefused

PREPARE: Final = "erp.finance.journals.prepare"
APPROVE: Final = "erp.finance.journals.approve"
PERIODS: Final = "erp.finance.periods.manage"
router = APIRouter(tags=["journals"])


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


def _functional_currency(db: Session, tenant: Tenant) -> tuple[str | None, str | None]:
    try:
        return policy.functional_currency(db, tenant.id), None
    except policy.FunctionalCurrencyError as exc:
        return None, str(exc)


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str) and key != "csrf_token"}


# --- preparer --------------------------------------------------------------


@router.get("/finance/journals")
def journals_register(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _page(
        request, "journals/register.html", _ctx(request, party, rows=queries.register(db, tenant_id=tenant.id))
    )


def _new_context(request: Request, db: Session, tenant: Tenant, party: Party, **extra: object) -> dict[str, object]:
    currency, currency_error = _functional_currency(db, tenant)
    return _ctx(
        request,
        party,
        accounts=queries.posting_accounts(db, tenant_id=tenant.id),
        currency=currency,
        currency_error=currency_error,
        line_numbers=range(1, forms.FORM_LINES + 1),
        values={"request_id": str(uuid4()), "kind": "STANDARD"},
        **extra,
    )


@router.get("/finance/journals/new")
def journal_new(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _page(request, "journals/new.html", _new_context(request, db, tenant, party))


@router.post("/finance/journals", response_model=None)
async def journal_create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    values = await _form(request)
    try:
        outcome = service.prepare_journal(db, tenant_id=tenant.id, actor=party.id, command=forms.parse_prepare(values))
    except JournalRefused as exc:
        context = _new_context(request, db, tenant, party, error=str(exc.detail or exc.code))
        context["values"] = values
        return _page(request, "journals/new.html", context, status_code=400)
    return _redirect(f"/finance/journals/{outcome.manual_id}")


@router.get("/finance/journals/held")
def held_register(
    request: Request,
    page: int = Query(1, ge=1, le=queries.MAX_PAGE),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _page(
        request, "journals/held.html", _ctx(request, party, **queries.held_journals(db, tenant_id=tenant.id, page=page))
    )


@router.get("/finance/journals/held/{journal_id}")
def held_review(
    journal_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    data = queries.held_detail(db, tenant_id=tenant.id, journal_id=journal_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Journal was not found")
    return _page(request, "journals/held_review.html", _ctx(request, party, **data))


@router.post("/finance/journals/held/{journal_id}/request-post", response_model=None)
async def held_request_post(
    journal_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    values = await _form(request)
    try:
        outcome = service.request_legacy_post(
            db, tenant_id=tenant.id, journal_id=journal_id, actor=party.id, review_ref=values.get("review_ref", "")
        )
    except JournalRefused as exc:
        data = queries.held_detail(db, tenant_id=tenant.id, journal_id=journal_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Journal was not found") from exc
        return _page(
            request,
            "journals/held_review.html",
            _ctx(request, party, **data, error=str(exc.detail or exc.code)),
            status_code=400,
        )
    return _redirect(f"/finance/journals/{outcome.manual_id}")


def _detail_response(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    manual_id: UUID,
    *,
    approver: bool,
    error: str | None = None,
) -> HTMLResponse:
    data = queries.detail(db, tenant_id=tenant.id, manual_id=manual_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Journal was not found")
    return _page(
        request,
        "journals/detail.html",
        _ctx(request, party, **data, approver=approver, error=error, reversal_default=date.today().isoformat()),
        status_code=400 if error else 200,
    )


@router.get("/finance/journals/{manual_id}")
def journal_detail(
    manual_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _detail_response(request, db, tenant, party, manual_id, approver=False)


@router.post("/finance/journals/{manual_id}/request-post", response_model=None)
def journal_request_post(
    manual_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    try:
        service.request_post(db, tenant_id=tenant.id, manual_id=manual_id, actor=party.id)
    except JournalRefused as exc:
        return _detail_response(
            request, db, tenant, party, manual_id, approver=False, error=str(exc.detail or exc.code)
        )
    return _redirect(f"/finance/journals/{manual_id}")


@router.post("/finance/journals/{manual_id}/request-reversal", response_model=None)
async def journal_request_reversal(
    manual_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    values = await _form(request)
    try:
        posting_date = date.fromisoformat(values.get("posting_date", ""))
    except ValueError:
        return _detail_response(
            request, db, tenant, party, manual_id, approver=False, error="Enter the reversal date as YYYY-MM-DD"
        )
    try:
        service.request_reversal(
            db,
            tenant_id=tenant.id,
            manual_id=manual_id,
            actor=party.id,
            reason=values.get("reason", ""),
            posting_date=posting_date,
        )
    except JournalRefused as exc:
        return _detail_response(
            request, db, tenant, party, manual_id, approver=False, error=str(exc.detail or exc.code)
        )
    return _redirect(f"/finance/journals/{manual_id}")


# --- approver --------------------------------------------------------------


@router.get("/finance/journal-approvals")
def approvals_queue(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _page(
        request, "journals/approvals.html", _ctx(request, party, rows=queries.approval_queue(db, tenant_id=tenant.id))
    )


@router.get("/finance/journal-approvals/{manual_id}")
def approval_detail(
    manual_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _detail_response(request, db, tenant, party, manual_id, approver=True)


def _approver_action(command: str):
    def handler(
        manual_id: UUID,
        request: Request,
        db: Session = Depends(get_db),
        tenant: Tenant = Depends(require_tenant),
        party: Party = Depends(require_web_permission(APPROVE)),
    ) -> HTMLResponse | RedirectResponse:
        try:
            getattr(service, command)(db, tenant_id=tenant.id, manual_id=manual_id, approver=party.id)
        except JournalRefused as exc:
            return _detail_response(
                request, db, tenant, party, manual_id, approver=True, error=str(exc.detail or exc.code)
            )
        return _redirect(f"/finance/journal-approvals/{manual_id}")

    return handler


router.add_api_route(
    "/finance/journal-approvals/{manual_id}/approve",
    _approver_action("approve_and_post"),
    methods=["POST"],
    response_model=None,
    name="journal_approve",
)
router.add_api_route(
    "/finance/journal-approvals/{manual_id}/approve-reversal",
    _approver_action("approve_reversal"),
    methods=["POST"],
    response_model=None,
    name="journal_approve_reversal",
)


@router.post("/finance/journal-approvals/{manual_id}/reject", response_model=None)
async def journal_reject(
    manual_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse | RedirectResponse:
    values = await _form(request)
    try:
        service.reject(db, tenant_id=tenant.id, manual_id=manual_id, actor=party.id, reason=values.get("reason", ""))
    except JournalRefused as exc:
        return _detail_response(request, db, tenant, party, manual_id, approver=True, error=str(exc.detail or exc.code))
    return _redirect(f"/finance/journal-approvals/{manual_id}")


# --- fiscal periods --------------------------------------------------------


def _periods_page(
    request: Request, db: Session, tenant: Tenant, party: Party, error: str | None = None
) -> HTMLResponse:
    currency, currency_error = _functional_currency(db, tenant)
    return _page(
        request,
        "journals/periods.html",
        _ctx(
            request,
            party,
            periods=queries.periods(db, tenant_id=tenant.id),
            currency=currency,
            currency_error=currency_error,
            error=error,
        ),
        status_code=400 if error else 200,
    )


@router.get("/finance/periods")
def periods_page(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PERIODS)),
) -> HTMLResponse:
    return _periods_page(request, db, tenant, party)


@router.post("/finance/periods/{period_id}/open", response_model=None)
def period_open(
    period_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PERIODS)),
) -> HTMLResponse | RedirectResponse:
    try:
        service.open_period(db, tenant_id=tenant.id, period_id=period_id, actor=party.id)
    except JournalRefused as exc:
        return _periods_page(request, db, tenant, party, str(exc.detail or exc.code))
    return _redirect("/finance/periods")


@router.post("/finance/periods/{period_id}/soft-close", response_model=None)
async def period_soft_close(
    period_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PERIODS)),
) -> HTMLResponse | RedirectResponse:
    values = await _form(request)
    try:
        service.soft_close_period(
            db, tenant_id=tenant.id, period_id=period_id, actor=party.id, review_ref=values.get("review_ref", "")
        )
    except JournalRefused as exc:
        return _periods_page(request, db, tenant, party, str(exc.detail or exc.code))
    return _redirect("/finance/periods")


@router.post("/finance/periods/{period_id}/lock", response_model=None)
def period_lock(
    period_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PERIODS)),
) -> HTMLResponse | RedirectResponse:
    try:
        service.lock_period(db, tenant_id=tenant.id, period_id=period_id, actor=party.id)
    except JournalRefused as exc:
        return _periods_page(request, db, tenant, party, str(exc.detail or exc.code))
    return _redirect("/finance/periods")


journals_web_feature = FeatureManifest(
    name="erp_journals",
    web_routers=(router,),
    audit_actions=service.AUDIT_ACTIONS,
    setting_domains=(str(policy.ACCOUNTING_POLICY_DOMAIN),),
)
