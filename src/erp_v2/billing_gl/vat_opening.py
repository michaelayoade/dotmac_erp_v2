"""Q1: load each migrated open invoice's remaining output-VAT deferral (1455) as an opening VAT position.

    python -m erp_v2.billing_gl vat-opening plan  --input I.json --receipt R.json
    ERP_BILLING_GL_VAT_OPENING_APPLY=1 python -m erp_v2.billing_gl vat-opening apply --input I.json \
        --receipt R.json --approved-plan-sha <sha> --operator-ref <who>

Runs at F7 after the #72 seal and T1, as the MIGRATION role (``MIGRATION_DATABASE_URL`` only, 0600 receipt created
before any read). Plan writes nothing. The input is the legacy as-recorded figures per invoice, one JSON item each:

    {"document_id": uuid, "treatment": str, "total": "grand_total", "tax": "legacy tax on the invoice",
     "open_initial": "billing initial_receivable", "journal_1455_credit": "...", "reclass": ["..."] | null,
     "deferral_account": uuid, "current_account": uuid, "legacy_ref": str}

``reclass`` = the legacy ``CUSTOMER_PAYMENT_VAT_RECLASS`` amounts attributed to the invoice by legacy's own prorate
over each payment's allocations (null = unattributable). D_k = journal_1455_credit - sum(reclass). An invoice whose D_k
is negative, above its tax, or unattributable is NOT loaded: it becomes an E38 report item and, per Q1, payments on it
BLOCK ``vat_position_missing`` until Finance resolves it. A replay is a no-op. The legacy extraction that produces the
input file is a separate reader step (probe P-B16-e); this module derives and loads, it does not read the legacy ERP.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from erp_v2.migration_ledger.common import (
    LedgerRefused,
    canonical_sha,
    fmt,
    money,
    refuse,
    sha_text,
    strict_json,
    text,
)
from erp_v2.migration_ledger.common import uuid_text as _uuid

APPLY_ENV = "ERP_BILLING_GL_VAT_OPENING_APPLY"
NS = UUID("7f1c1d34-0c58-5b1e-9a58-0a6f4f2f7e11")
ITEM_KEYS = {
    "document_id",
    "treatment",
    "total",
    "tax",
    "open_initial",
    "journal_1455_credit",
    "reclass",
    "deferral_account",
    "current_account",
    "legacy_ref",
    "finance_override",
}


def derive(item: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """(D_k, None) when loadable, else (None, E38 reason). Pure.

    A ``finance_override`` is the governed Finance resolution of an E38 item: a stated remaining deferral with its
    evidence sha, bounded by the invoice's tax, replacing the legacy derivation for that one invoice.
    """
    override = item.get("finance_override")
    if override is not None:
        if override["deferred"] < 0 or override["deferred"] > item["tax"]:
            return None, "override_out_of_range"
        return override["deferred"], None
    if item["reclass"] is None:
        return None, "unattributable"
    remaining = item["journal_1455_credit"] - sum(item["reclass"], Decimal(0))
    if remaining < 0:
        return None, "negative_remaining"
    if remaining > item["tax"]:
        return None, "remaining_above_tax"
    return remaining, None


def _override(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"deferred", "evidence_sha256", "ref"}:
        refuse("invalid_finance_override")
    return {
        "deferred": money(raw["deferred"]),
        "evidence_sha256": sha_text(raw["evidence_sha256"], "invalid_finance_override"),
        "ref": text(raw["ref"], "invalid_finance_override"),
    }


def parse(raw: bytes) -> dict[str, Any]:
    body = strict_json(raw)
    if not isinstance(body, dict) or set(body) != {"version", "tenant_id", "items"} or body["version"] != 1:
        refuse("invalid_vat_opening_input")
    tenant = _uuid(body["tenant_id"], "invalid_tenant_id")
    items = []
    seen = set()
    for raw_item in body["items"]:
        if not isinstance(raw_item, dict) or set(raw_item) != ITEM_KEYS:
            refuse("invalid_vat_opening_item")
        reclass = raw_item["reclass"]
        item = {
            "document_id": _uuid(raw_item["document_id"], "invalid_document_id"),
            "treatment": text(raw_item["treatment"], "invalid_treatment", 80),
            "total": money(raw_item["total"]),
            "tax": money(raw_item["tax"]),
            "open_initial": money(raw_item["open_initial"]),
            "journal_1455_credit": money(raw_item["journal_1455_credit"]),
            "reclass": None if reclass is None else [money(x) for x in reclass],
            "deferral_account": None
            if raw_item["deferral_account"] is None
            else _uuid(raw_item["deferral_account"], "invalid_account"),
            "current_account": None
            if raw_item["current_account"] is None
            else _uuid(raw_item["current_account"], "invalid_account"),
            "legacy_ref": text(raw_item["legacy_ref"], "invalid_legacy_ref"),
            "finance_override": _override(raw_item["finance_override"]),
        }
        if item["tax"] != 0 and (item["deferral_account"] is None or item["current_account"] is None):
            refuse("invalid_account")
        key = (item["document_id"], item["treatment"])
        if key in seen:
            refuse("duplicate_vat_opening_item")
        seen.add(key)
        if item["total"] <= 0 or item["open_initial"] < 0 or item["open_initial"] > item["total"]:
            refuse("invalid_vat_opening_amounts")
        items.append(item)
    return {"tenant_id": tenant, "items": items}


def billing_check(db, tenant_id: UUID):
    """A per-item cross-check of the input against billing: the document exists, is THIS tenant's imported open item,
    its total equals the original grand total and its current exposure equals ``open_initial`` (the loader runs before
    any live payment). A wrong ``open_initial`` would give a permanent ``vat_invariant`` or no close-out."""
    from dotmac_billing import get_document_exposure
    from dotmac_billing.models import BillingDocument
    from dotmac_kernel.cache import TenantScope
    from sqlalchemy import select

    scope = TenantScope(tenant_id)

    def check(item: dict[str, Any]) -> str | None:
        document = db.scalar(
            select(BillingDocument).where(
                BillingDocument.tenant_id == tenant_id, BillingDocument.id == item["document_id"]
            )
        )
        if document is None:
            return "document_unresolved"
        if document.authoring_mode != "imported_open_item" or document.document_kind != "invoice":
            return "document_not_imported_open_item"
        if Decimal(document.grand_total) != item["total"]:
            return "total_mismatch"
        try:
            exposure = get_document_exposure(db, scope=scope, document_id=item["document_id"])
        except Exception:  # an unreadable exposure is an item to resolve, not a crash
            return "exposure_unreadable"
        if Decimal(exposure.collectible_receivable.amount) != item["open_initial"]:
            return "open_initial_mismatch"
        return None

    return check


def configured_treatments(db, tenant_id: UUID) -> frozenset[str]:
    """The treatment codes of the tenant's ``tax_accounts`` setting: the same set the mapper resolves for live
    invoices. Billing a13 exposes no treatment code for an imported open item (no tax snapshot is stored), so this
    configured set, plus the operator treatment map whose sha the extract receipt records, is the control."""
    from dotmac_kernel.settings_resolver import resolve_value

    from erp_v2.billing_gl import settings

    value = resolve_value(db, settings.DOMAIN, "tax_accounts", tenant_id=tenant_id, default={})
    return frozenset(value) if isinstance(value, dict) else frozenset()


def plan(
    parsed: dict[str, Any], input_sha: str, *, check: Callable[[dict[str, Any]], str | None], treatments: frozenset[str]
) -> dict[str, Any]:
    """Both gates are required (a gate must not be relaxable by omission). ``treatments`` is the configured set every
    item's treatment must belong to; an item outside it is an E38 item (``treatment_not_in_settings``), never loaded
    and never dropped. ``check`` is the billing cross-check (``billing_check``)."""
    load, e38 = [], []
    for item in parsed["items"]:
        remaining, reason = derive(item)
        if reason is None and item["treatment"] not in treatments:
            reason = "treatment_not_in_settings"
        if reason is None:
            reason = check(item)
        row = {
            "document_id": str(item["document_id"]),
            "treatment": item["treatment"],
            "legacy_ref": item["legacy_ref"],
        }
        if reason is not None:
            e38.append({**row, "reason": reason})
            continue
        load.append(
            {
                **row,
                "deferred": fmt(remaining),
                "total": fmt(item["total"]),
                "open_initial": fmt(item["open_initial"]),
                "deferral_account": None if item["deferral_account"] is None else str(item["deferral_account"]),
                "current_account": None if item["current_account"] is None else str(item["current_account"]),
                "override_evidence_sha256": None
                if item["finance_override"] is None
                else item["finance_override"]["evidence_sha256"],
            }
        )
    body = {"input_sha256": input_sha, "load": load, "e38": e38}
    overridden = sum(1 for row in load if row["override_evidence_sha256"] is not None)
    return {
        **body,
        "counts": {"load": len(load), "e38": len(e38), "override": overridden},
        "plan_sha256": canonical_sha(body),
    }


def refuse_after_flip(db, tenant_id: UUID, document_ids: list[UUID]) -> None:
    """The loader runs BEFORE any live payment (F7). ``open_initial`` is the exposure at the opening, so loading after
    a payment would set it to the post-payment exposure and the blocked allocations would then release twice.

    Refuses when the mapper is enabled (F10 has happened), when the tenant has any POSTED or pending mapper fact, or
    when billing already holds an allocation or an opening-lot application against a listed document.
    """
    from dotmac_billing.models import AllocationEffect, OpeningCashEffect
    from sqlalchemy import select

    from erp_v2.billing_gl import settings
    from erp_v2.billing_gl.models import PENDING, POSTED, FactPosting

    if settings.load(db, tenant_id).enabled:
        refuse("vat_opening_after_flip", "billing_gl.enabled is true")
    if db.scalar(
        select(FactPosting.id)
        .where(FactPosting.tenant_id == tenant_id, FactPosting.disposition.in_((POSTED, *PENDING)))
        .limit(1)
    ):
        refuse("vat_opening_after_flip", "the mapper has live or pending facts")
    if document_ids and (
        db.scalar(
            select(AllocationEffect.id)
            .where(AllocationEffect.tenant_id == tenant_id, AllocationEffect.document_id.in_(document_ids))
            .limit(1)
        )
        or db.scalar(
            select(OpeningCashEffect.id)
            .where(OpeningCashEffect.tenant_id == tenant_id, OpeningCashEffect.document_id.in_(document_ids))
            .limit(1)
        )
    ):
        refuse("vat_opening_after_flip", "a listed document already has a payment")


def apply(db, tenant_id: UUID, plan_body: dict[str, Any]) -> dict[str, int]:
    """Insert positions and their opening ``defer`` movements as the migration role. A replay writes nothing.

    Refuses after the flip (see ``refuse_after_flip``).
    """
    from sqlalchemy import select

    from erp_v2.billing_gl.models import ORIGIN_OPENING, VatMovement, VatPosition

    refuse_after_flip(db, tenant_id, [UUID(item["document_id"]) for item in plan_body["load"]])

    written = skipped = 0
    for item in plan_body["load"]:
        document_id = UUID(item["document_id"])
        found = db.scalar(
            select(VatPosition.id).where(
                VatPosition.tenant_id == tenant_id,
                VatPosition.document_id == document_id,
                VatPosition.treatment_code == item["treatment"],
            )
        )
        if found is not None:
            skipped += 1
            continue
        position = VatPosition(
            tenant_id=tenant_id,
            document_id=document_id,
            treatment_code=item["treatment"],
            deferral_account_id=None if item["deferral_account"] is None else UUID(item["deferral_account"]),
            current_account_id=None if item["current_account"] is None else UUID(item["current_account"]),
            origin=ORIGIN_OPENING,
            total=Decimal(item["total"]),
            deferred=Decimal(item["deferred"]),
            open_initial=Decimal(item["open_initial"]),
            source=plan_body["plan_sha256"],
        )
        db.add(position)
        db.flush()
        db.add(
            VatMovement(
                tenant_id=tenant_id,
                position_id=position.id,
                fact_id=uuid5(NS, f"{document_id}:{item['treatment']}"),
                fact_version=0,
                kind="defer",
                deferral_delta=Decimal(item["deferred"]),
                current_delta=Decimal(0),
                allocation_ref="opening",
            )
        )
        db.flush()
        written += 1
    return {"written": written, "already_loaded": skipped}


def main(argv: list[str] | None = None) -> int:
    from erp_v2.migration_ledger import cli_common

    parser = argparse.ArgumentParser(prog="erp_v2.billing_gl vat-opening")
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--approved-plan-sha")
    parser.add_argument("--operator-ref")
    args = parser.parse_args(argv)

    def pre() -> None:
        if args.action == "apply" and (os.environ.get(APPLY_ENV) != "1" or not args.approved_plan_sha):
            refuse("explicit_vat_opening_apply_required")

    def body() -> dict[str, Any]:
        raw, sha = cli_common.read_bound(args.input)
        parsed = parse(raw)
        import sqlalchemy as sa
        from sqlalchemy.orm import Session

        if args.action == "apply":
            cli_common.operator(args.operator_ref)
        engine = cli_common.engine()
        with Session(engine) as db:
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(parsed["tenant_id"])})
            planned = plan(
                parsed,
                sha,
                check=billing_check(db, parsed["tenant_id"]),
                treatments=configured_treatments(db, parsed["tenant_id"]),
            )
            if args.action == "plan":
                db.rollback()
                return {"ready": True, "action": "plan", **planned}
            if args.approved_plan_sha != planned["plan_sha256"]:
                refuse("plan_sha_mismatch")
            result = apply(db, parsed["tenant_id"], planned)
            db.commit()
        return {"ready": True, "action": "apply", "plan_sha256": planned["plan_sha256"], **result}

    try:
        return cli_common.run(args.receipt, body, "vat-opening", pre=pre)
    except LedgerRefused:
        return 2


if __name__ == "__main__":
    sys.exit(main())
