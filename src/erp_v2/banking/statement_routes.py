"""Bank statement upload, preview and import (B6). Browser routes, permission-gated.

Dormant until a party is granted ``erp.banking.statements.import`` (the P0
``bank_reconciler`` role; admin and staff hold nothing, W14). The upload is read
into memory only, validated and parsed as data; it is never written to disk by this
code, never executed and never echoed back. No provider polling and no Mono receiver
is involved: day one is manual statement import only (W4).

Preview and import are two separate uploads of the same file: the import form
carries the previewed sha256 and refuses when the re-uploaded bytes differ.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from dotmac_banking.models import BankAccount
from dotmac_kernel.deps import get_db, require_tenant
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.templating import render
from dotmac_kernel.web_deps import require_web_permission
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from erp_v2 import ui
from erp_v2.banking import statement_import as service
from erp_v2.banking.statement_file import DATE_FORMATS, MAX_FILE_BYTES

IMPORT_PERMISSION = "erp.banking.statements.import"
FORM_OVERHEAD_BYTES = 64 * 1024
ALLOWED_TYPES = {
    "csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
}
PREVIEW_ROWS = 50
_DECIMAL = re.compile(r"^-?[0-9]{1,14}(\.[0-9]{1,6})?$")
_GROUPED = re.compile(r"^-?[0-9]{1,3}(,[0-9]{3})+(\.[0-9]{1,6})?$")

router = APIRouter(tags=["banking-statements-web"])


def _ctx(request: Request, party: Party, **extra: object) -> dict[str, object]:
    return {
        **ui.template_globals(),
        "current_email": party.email,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "date_formats": tuple(DATE_FORMATS),
        "max_mib": MAX_FILE_BYTES // (1024 * 1024),
        **extra,
    }


def _page(request: Request, template: str, context: dict[str, object], status_code: int = 200) -> HTMLResponse:
    response = render(request, template, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _accounts(db: Session, tenant: Tenant) -> list[BankAccount]:
    return list(
        db.scalars(
            select(BankAccount)
            .where(BankAccount.tenant_id == tenant.id, BankAccount.status == "active")
            .order_by(BankAccount.account_code)
            .limit(200)
        )
    )


def _optional_decimal(value: object, label: str) -> Decimal | None:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    if not (_DECIMAL.match(text) or _GROUPED.match(text)):
        raise service.StatementImportRefused([f"{label} is not a valid amount"])
    return Decimal(text.replace(",", ""))


async def _read(request: Request) -> tuple[dict[str, str], bytes, str]:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_FILE_BYTES + FORM_OVERHEAD_BYTES):
        raise HTTPException(413, "Upload is too large")
    form = await request.form()
    fields = {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf_token"}
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise service.StatementImportRefused(["Choose a statement file to upload"])
    name = (upload.filename or "").lower()
    ext = "xlsx" if name.endswith(".xlsx") else "csv" if name.endswith(".csv") else ""
    if not ext:
        raise service.StatementImportRefused(["Only .csv and .xlsx statement files are accepted"])
    media = (upload.content_type or "").split(";")[0].strip().lower()
    if media not in ALLOWED_TYPES[ext]:
        raise service.StatementImportRefused(["The file content type does not match a CSV or XLSX statement"])
    data = await upload.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise service.StatementImportRefused([f"File exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MiB limit"])
    return fields, data, ext


def _options(fields: dict[str, str]) -> service.ImportOptions:
    try:
        account_id = UUID(fields.get("account_id", ""))
    except ValueError:
        raise service.StatementImportRefused(["Choose a bank account"]) from None
    return service.ImportOptions(
        account_id=account_id,
        date_format=fields.get("date_format", "iso"),
        statement_ref=fields.get("statement_ref") or None,
        opening_balance=_optional_decimal(fields.get("opening_balance"), "Opening balance"),
        closing_balance=_optional_decimal(fields.get("closing_balance"), "Closing balance"),
    )


@router.get("/finance/banking/statements/import")
def statement_import_form(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(IMPORT_PERMISSION)),
) -> HTMLResponse:
    return _page(
        request, "banking/statement_import.html", _ctx(request, party, accounts=_accounts(db, tenant), values={})
    )


@router.post("/finance/banking/statements/preview", response_model=None)
async def statement_preview(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(IMPORT_PERMISSION)),
) -> HTMLResponse:
    values: dict[str, str] = {}
    try:
        fields, data, ext = await _read(request)
        values = fields
        plan = service.preview(
            db,
            tenant_id=tenant.id,
            data=data,
            file_format=ext,
            options=_options(fields),
            now=datetime.now(UTC),
        )
    except service.StatementImportRefused as exc:
        return _page(
            request,
            "banking/statement_import.html",
            _ctx(request, party, accounts=_accounts(db, tenant), values=values, errors=exc.reasons),
            status_code=400,
        )
    return _page(
        request,
        "banking/statement_preview.html",
        _ctx(
            request,
            party,
            plan=plan,
            values=values,
            sample=plan.parsed.lines[:PREVIEW_ROWS],
            duplicate_sample=plan.duplicates[:20],
        ),
    )


@router.post("/finance/banking/statements/import", response_model=None)
async def statement_import(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    party: Party = Depends(require_web_permission(IMPORT_PERMISSION)),
) -> HTMLResponse | RedirectResponse:
    values: dict[str, str] = {}
    try:
        fields, data, ext = await _read(request)
        values = fields
        service.import_statement(
            db,
            tenant_id=tenant.id,
            actor_party_id=party.id,
            data=data,
            file_format=ext,
            expected_sha256=fields.get("expected_sha256", ""),
            options=_options(fields),
            now=datetime.now(UTC),
        )
    except service.StatementImportRefused as exc:
        return _page(
            request,
            "banking/statement_import.html",
            _ctx(request, party, accounts=_accounts(db, tenant), values=values, errors=exc.reasons),
            status_code=400,
        )
    return RedirectResponse("/finance/banking", status_code=303)


banking_statements_feature = FeatureManifest(
    name="erp_banking_statements",
    web_routers=(router,),
    audit_actions=(service.AUDIT_STATEMENT_IMPORTED,),
)
