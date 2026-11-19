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
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from erp_v2.migration_ledger.common import LedgerRefused, canonical_sha, fmt, money, refuse, strict_json, text
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
}


def derive(item: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """(D_k, None) when loadable, else (None, E38 reason). Pure."""
    if item["reclass"] is None:
        return None, "unattributable"
    remaining = item["journal_1455_credit"] - sum(item["reclass"], Decimal(0))
    if remaining < 0:
        return None, "negative_remaining"
    if remaining > item["tax"]:
        return None, "remaining_above_tax"
    return remaining, None


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
            "deferral_account": _uuid(raw_item["deferral_account"], "invalid_account"),
            "current_account": _uuid(raw_item["current_account"], "invalid_account"),
            "legacy_ref": text(raw_item["legacy_ref"], "invalid_legacy_ref"),
        }
        key = (item["document_id"], item["treatment"])
        if key in seen:
            refuse("duplicate_vat_opening_item")
        seen.add(key)
        if item["total"] <= 0 or item["open_initial"] < 0 or item["open_initial"] > item["total"]:
            refuse("invalid_vat_opening_amounts")
        items.append(item)
    return {"tenant_id": tenant, "items": items}


def plan(parsed: dict[str, Any], input_sha: str) -> dict[str, Any]:
    load, e38 = [], []
    for item in parsed["items"]:
        remaining, reason = derive(item)
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
                "deferral_account": str(item["deferral_account"]),
                "current_account": str(item["current_account"]),
            }
        )
    body = {"input_sha256": input_sha, "load": load, "e38": e38}
    return {**body, "counts": {"load": len(load), "e38": len(e38)}, "plan_sha256": canonical_sha(body)}


def apply(db, tenant_id: UUID, plan_body: dict[str, Any]) -> dict[str, int]:
    """Insert positions and their opening ``defer`` movements as the migration role. A replay writes nothing."""
    from sqlalchemy import select

    from erp_v2.billing_gl.models import ORIGIN_OPENING, VatMovement, VatPosition

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
            deferral_account_id=UUID(item["deferral_account"]),
            current_account_id=UUID(item["current_account"]),
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
        planned = plan(parsed, sha)
        if args.action == "plan":
            return {"ready": True, "action": "plan", **planned}
        if args.approved_plan_sha != planned["plan_sha256"]:
            refuse("plan_sha_mismatch")
        cli_common.operator(args.operator_ref)
        import sqlalchemy as sa
        from sqlalchemy.orm import Session

        engine = cli_common.engine()
        with Session(engine) as db:
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(parsed["tenant_id"])})
            result = apply(db, parsed["tenant_id"], planned)
            db.commit()
        return {"ready": True, "action": "apply", "plan_sha256": planned["plan_sha256"], **result}

    try:
        return cli_common.run(args.receipt, body, "vat-opening", pre=pre)
    except LedgerRefused:
        return 2


if __name__ == "__main__":
    sys.exit(main())
