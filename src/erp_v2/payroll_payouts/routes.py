"""Payroll payout routes (B5). Every route is permission-gated and dormant until a named party
holds the role (access_cli); admin and staff hold none of these permissions (W14).

- prepare   (finance_preparer)  /payroll/payouts                 prepare a net-pay batch or statutory remittance
- approve   (finance_approver)  /payroll/payout-approvals        approve or cancel
- treasury  (treasury)          /payroll/payout-treasury         record as paid; optional bank file (net pay only)

None of these are facet entry routes: the facet admission permission also applies. Nothing here calls a bank.
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
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.disbursement import file as bank_file
from erp_v2.disbursement import policy as disbursement_policy
from erp_v2.journals import policy as journal_policy
from erp_v2.payroll_payouts import queries, service
from erp_v2.payroll_payouts.models import APPROVED, DRAFT, FILE_ISSUED, KIND_NET_PAY, KIND_STATUTORY, RECORDED
from erp_v2.payroll_payouts.service import PayoutRefused

PREPARE: Final = "erp.finance.payroll_payouts.prepare"
APPROVE: Final = "erp.finance.payroll_payouts.approve"
RECORD: Final = "erp.treasury.payroll_payouts.record"
GENERATE: Final = "erp.treasury.disbursement_files.generate"

PREPARE_BASE: Final = "/payroll/payouts"
APPROVE_BASE: Final = "/payroll/payout-approvals"
TREASURY_BASE: Final = "/payroll/payout-treasury"

# route name -> permission, asserted against the access profile in tests.
ROUTE_PERMISSIONS: Final = {
    "register": PREPARE,
    "new": PREPARE,
    "create": PREPARE,
    "prepare_detail": PREPARE,
    "approval_queue": APPROVE,
    "approval_detail": APPROVE,
    "approve": APPROVE,
    "cancel": APPROVE,
    "treasury_queue": RECORD,
    "treasury_detail": RECORD,
    "record": RECORD,
    "bank_file": GENERATE,
}

router = APIRouter(tags=["payroll-payouts"])


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


def _message(exc: PayoutRefused) -> str:
    return f"{exc.code}: {exc}"


def _list_page(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    *,
    title: str,
    detail_base: str,
    statuses: tuple[str, ...] | None,
    extra: dict[str, object] | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return _page(
        request,
        "payroll_payouts/list.html",
        _ctx(
            request,
            party,
            title=title,
            detail_base=detail_base,
            rows=queries.register(db, tenant_id=tenant.id, statuses=statuses),
            error=error,
            **(extra or {}),
        ),
        status_code=400 if error else 200,
    )


def _detail_page(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    batch_id: UUID,
    *,
    mode: str,
    back: str,
    error: str | None = None,
) -> HTMLResponse:
    data = queries.detail(db, tenant_id=tenant.id, batch_id=batch_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Payout batch was not found")
    return _page(
        request,
        "payroll_payouts/detail.html",
        _ctx(
            request,
            party,
            **data,
            mode=mode,
            back=back,
            bases={"approve": APPROVE_BASE, "treasury": TREASURY_BASE},
            bank_file_enabled=disbursement_policy.bank_file_generation_enabled(db, tenant.id),
            DRAFT=DRAFT,
            APPROVED=APPROVED,
            FILE_ISSUED=FILE_ISSUED,
            RECORDED=RECORDED,
            NET_PAY=KIND_NET_PAY,
            error=error,
        ),
        status_code=400 if error else 200,
    )


async def _values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str) and key != "csrf_token"}


# --- preparer --------------------------------------------------------------


@router.get(PREPARE_BASE)
def register(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _list_page(
        request,
        db,
        tenant,
        party,
        title="Payroll payouts",
        detail_base=PREPARE_BASE,
        statuses=None,
        extra={"new_link": f"{PREPARE_BASE}/new"},
    )


def _new_context(
    request: Request,
    db: Session,
    tenant: Tenant,
    party: Party,
    *,
    run_id: UUID | None,
    kind: str,
    error: str | None = None,
) -> dict[str, object]:
    try:
        currency = journal_policy.functional_currency(db, tenant.id)
        currency_error = None
    except journal_policy.FunctionalCurrencyError as exc:
        currency, currency_error = None, str(exc)
    return _ctx(
        request,
        party,
        currency=currency,
        currency_error=currency_error,
        runs=queries.finalized_runs(db, tenant_id=tenant.id),
        run_id=run_id,
        kind=kind,
        accounts=queries.bank_accounts(db, tenant_id=tenant.id, currency=currency) if currency else [],
        liabilities=queries.candidates(db, tenant_id=tenant.id, run_id=run_id, kind=kind) if run_id else [],
        request_id=str(uuid4()),
        today=date.today().isoformat(),
        error=error,
    )


@router.get(f"{PREPARE_BASE}/new")
def new(
    request: Request,
    run_id: UUID | None = None,
    kind: str = KIND_NET_PAY,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    kind = kind if kind in (KIND_NET_PAY, KIND_STATUTORY) else KIND_NET_PAY
    return _page(
        request,
        "payroll_payouts/new.html",
        _new_context(request, db, tenant, party, run_id=run_id, kind=kind),
    )


@router.post(PREPARE_BASE, response_model=None)
async def create(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    items = [(k, v) for k, v in form.multi_items() if isinstance(v, str) and k != "csrf_token"]
    values = dict(items)
    kind = values.get("kind", KIND_NET_PAY)
    run_id: UUID | None = None
    try:
        try:
            request_id = UUID(values.get("request_id", ""))
            run_id = UUID(values.get("run_id", ""))
            bank_account_id = UUID(values.get("bank_account_id", ""))
            liability_ids = [UUID(v) for k, v in items if k == "liability_id"] or None
        except ValueError as exc:
            raise PayoutRefused("invalid_input", "The form has expired or is incomplete; reload it") from exc
        try:
            value_date = date.fromisoformat(values.get("value_date", ""))
        except ValueError as exc:
            raise PayoutRefused("invalid_input", "Enter the value date as YYYY-MM-DD") from exc
        outcome = service.prepare_batch(
            db,
            tenant=tenant,
            party=party,
            request_id=request_id,
            run_id=run_id,
            kind=kind,
            bank_account_id=bank_account_id,
            value_date=value_date,
            destination_ref=values.get("destination_ref"),
            liability_ids=liability_ids,
        )
    except PayoutRefused as exc:
        return _page(
            request,
            "payroll_payouts/new.html",
            _new_context(request, db, tenant, party, run_id=run_id, kind=kind, error=_message(exc)),
            400,
        )
    return _redirect(f"{PREPARE_BASE}/{outcome.batch_id}")


@router.get(PREPARE_BASE + "/{batch_id}")
def prepare_detail(
    batch_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(PREPARE)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, batch_id, mode="prepare", back=PREPARE_BASE)


# --- approver --------------------------------------------------------------


@router.get(APPROVE_BASE)
def approval_queue(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _list_page(
        request,
        db,
        tenant,
        party,
        title="Payroll payouts awaiting approval",
        detail_base=APPROVE_BASE,
        statuses=(DRAFT, APPROVED, FILE_ISSUED),
    )


@router.get(APPROVE_BASE + "/{batch_id}")
def approval_detail(
    batch_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, batch_id, mode="approve", back=APPROVE_BASE)


@router.post(APPROVE_BASE + "/{batch_id}/approve", response_model=None)
def approve(
    batch_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse | RedirectResponse:
    try:
        service.approve_batch(db, tenant=tenant, party=party, batch_id=batch_id)
    except PayoutRefused as exc:
        return _detail_page(
            request, db, tenant, party, batch_id, mode="approve", back=APPROVE_BASE, error=_message(exc)
        )
    return _redirect(f"{APPROVE_BASE}/{batch_id}")


@router.post(APPROVE_BASE + "/{batch_id}/cancel", response_model=None)
async def cancel(
    batch_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(APPROVE)),
) -> HTMLResponse | RedirectResponse:
    values = await _values(request)
    try:
        service.cancel_batch(
            db,
            tenant=tenant,
            party=party,
            batch_id=batch_id,
            reason=values.get("reason", ""),
            recall_reference=values.get("recall_reference", ""),
        )
    except PayoutRefused as exc:
        return _detail_page(
            request, db, tenant, party, batch_id, mode="approve", back=APPROVE_BASE, error=_message(exc)
        )
    return _redirect(f"{APPROVE_BASE}/{batch_id}")


# --- treasury --------------------------------------------------------------


@router.get(TREASURY_BASE)
def treasury_queue(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(RECORD)),
) -> HTMLResponse:
    return _list_page(
        request,
        db,
        tenant,
        party,
        title="Approved payroll payouts to record",
        detail_base=TREASURY_BASE,
        statuses=(APPROVED, FILE_ISSUED),
    )


@router.get(TREASURY_BASE + "/{batch_id}")
def treasury_detail(
    batch_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(RECORD)),
) -> HTMLResponse:
    return _detail_page(request, db, tenant, party, batch_id, mode="treasury", back=TREASURY_BASE)


@router.post(TREASURY_BASE + "/{batch_id}/record", response_model=None)
async def record(
    batch_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(RECORD)),
) -> HTMLResponse | RedirectResponse:
    values = await _values(request)
    try:
        service.record_batch(
            db, tenant=tenant, party=party, batch_id=batch_id, bank_reference=values.get("bank_reference", "")
        )
    except PayoutRefused as exc:
        return _detail_page(
            request, db, tenant, party, batch_id, mode="treasury", back=TREASURY_BASE, error=_message(exc)
        )
    return _redirect(f"{TREASURY_BASE}/{batch_id}")


@router.post(TREASURY_BASE + "/{batch_id}/bank-file", response_model=None)
async def bank_file_download(
    batch_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(GENERATE)),
) -> Response | HTMLResponse:
    try:
        built = service.generate_bank_file(db, tenant=tenant, party=party, batch_id=batch_id)
    except PayoutRefused as exc:
        if exc.code == "bank_file_disabled":
            raise HTTPException(status_code=404, detail="Not found") from exc
        return _detail_page(
            request, db, tenant, party, batch_id, mode="treasury", back=TREASURY_BASE, error=_message(exc)
        )
    # The receipts and audit row must be durable before the bytes leave the process.
    db.commit()
    return Response(
        content=built.file.content,
        media_type="text/csv; charset=utf-8",
        headers=bank_file.no_store_headers(built.filename),
    )


payroll_payouts_feature = FeatureManifest(
    name="erp_payroll_payouts",
    web_routers=(router,),
    # The treasury_policy setting domain is owned by erp_supplier_payments (one owner per domain).
    audit_actions=service.AUDIT_ACTIONS,
)
