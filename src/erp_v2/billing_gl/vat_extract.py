"""Q1 / P-B16-e: derive each carried invoice's remaining output-VAT deferral (1455) from the legacy snapshot.

    python -m erp_v2.billing_gl vat-extract --tenant-id T --organization-id O --cutoff YYYY-MM-DD \
        --snapshot-sha256 S --out ITEMS.json --receipt R.json [--overrides OVERRIDES.json] [--treatment-map MAP.json]

Reads ONLY (env-only DSNs, both connections read-only): ``MIGRATION_DATABASE_URL`` for the destination (the carried
invoices) and ``LEGACY_DATABASE_URL`` for the rehearsal snapshot. Writes two 0600 files created exclusively before any
read: the sealed items file (exactly the ``vat-opening`` input, canonical JSON, deterministic: a re-run on the same
snapshot gives the same sha) and a counts-only receipt (no invoice id, no per-invoice amount).

Derivation (legacy as recorded, never recomputed policy):
* GL truth is ``gl.posted_ledger_line`` on the deferral accounts (``deferral_pair_account_id`` of each VAT/GST tax
  code's collected account) up to the cutoff. Reversal journals keep their original's source key, so reversed and
  voided invoices and bounced payments net out by construction.
* the 1455 credit of an invoice = the net of its lines from the INVOICE, AR_INVOICE_VAT_DEFERRAL and
  AR_INVOICE_RESTORE journals (POSTED journals only: that is what the ledger table holds);
* the reclass of an invoice = legacy's own prorate (HALF_EVEN to 0.01 per allocation per tax line) over each payment's
  allocations, counted only for payments that have a net CUSTOMER_PAYMENT_VAT_RECLASS journal at the cutoff; a payment
  whose recomputed total differs from its journal makes every invoice it touches unattributable (E38), never guessed;
* the legacy invoice -> billing document link is the #72 import receipt (``opening_receivable_bases`` of the receipts
  in ``erp_ar_opening_admissions``), never an invoice number;
* every carried invoice appears exactly once, zero-tax ones as a zero position; NGN only;
* the tie is exact: GL(deferral accounts) = sum of per-invoice remaining + (derived reclass - journal reclass) + the
  lines no invoice or payment owns (manual journals, AP). Any difference refuses the run.
Finance overrides are a SEPARATE named input with evidence; they never come from this derivation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from erp_v2.billing_gl import vat_opening
from erp_v2.migration_ledger.common import LedgerRefused, fmt, money, refuse, sha_text, strict_json, text

CENT = Decimal("0.01")
ZERO = Decimal(0)
FUNCTIONAL = "NGN"
INVOICE_SOURCES = frozenset({"INVOICE", "AR_INVOICE_VAT_DEFERRAL", "AR_INVOICE_RESTORE"})
RECLASS_SOURCE = "CUSTOMER_PAYMENT_VAT_RECLASS"
NO_TREATMENT = "none"


@dataclass(frozen=True, slots=True)
class TaxInfo:
    code: str
    deferral: str | None
    current: str | None

    @property
    def deferrable(self) -> bool:
        return bool(self.deferral and self.current and self.deferral != self.current)


@dataclass(frozen=True, slots=True)
class GlLine:
    account: str
    source_module: str | None
    source_type: str | None
    source_id: str | None
    net_credit: Decimal
    currency: str


@dataclass(slots=True)
class Invoice:
    number: str
    total: Decimal
    currency: str
    lines: list[tuple[str | None, Decimal]] = field(default_factory=list)  # (tax_code_id, line tax_amount)
    line_taxes: list[tuple[str, Decimal]] = field(default_factory=list)  # (tax_code_id, tax_amount)


@dataclass(slots=True)
class Legacy:
    tax: dict[str, TaxInfo]
    gl: list[GlLine]
    invoices: dict[str, Invoice]
    allocations: list[tuple[str, str, Decimal]]  # (payment_id, invoice_id, allocated_amount)


@dataclass(frozen=True, slots=True)
class Carried:
    invoice_key: str
    document_id: UUID
    open_initial: Decimal
    currency: str


def prorate(allocated: Decimal, component: Decimal, total: Decimal) -> Decimal:
    """Legacy ``prorate_amount``: (allocated x component / total) quantized to 0.01, HALF_EVEN; 0 when total is 0."""
    if total == 0:
        return ZERO
    return ((allocated * component) / total).quantize(CENT, rounding=ROUND_HALF_EVEN)


def derive(
    tenant_id: UUID, carried: list[Carried], data: Legacy, treatment_map: dict[str, str] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The sealed input body and the counts-only report. Pure: refuses rather than guesses."""
    treatment_map = treatment_map or {}
    if any(c.currency != FUNCTIONAL for c in carried) or any(g.currency != FUNCTIONAL for g in data.gl):
        refuse("non_ngn_unsupported")
    if len({c.invoice_key for c in carried}) != len(carried) or len({c.document_id for c in carried}) != len(carried):
        refuse("carried_invoice_duplicated")

    def treatment(tax_id: str) -> str:
        code = data.tax[tax_id].code
        return treatment_map.get(code, code)

    # 1. classify every GL line exactly once
    invoice_credit: dict[str, Decimal] = defaultdict(Decimal)
    reclass_net: dict[str, Decimal] = defaultdict(Decimal)  # payment id -> net debit (positive = recognised)
    other: dict[str, Decimal] = defaultdict(Decimal)
    gl_total = ZERO
    for line in data.gl:
        gl_total += line.net_credit
        if line.source_module == "AR" and line.source_type in INVOICE_SOURCES and line.source_id:
            invoice_credit[line.source_id] += line.net_credit
        elif line.source_module == "AR" and line.source_type == RECLASS_SOURCE and line.source_id:
            reclass_net[line.source_id] += -line.net_credit
        else:
            other[f"{line.source_module}/{line.source_type}"] += line.net_credit

    # 2. legacy's own prorate over each counted payment's allocations
    derived_by: dict[tuple[str, str], list[Decimal]] = defaultdict(list)  # (invoice, treatment) -> amounts
    derived_payment: dict[str, Decimal] = defaultdict(Decimal)
    touched: dict[str, set[str]] = defaultdict(set)  # payment -> invoices
    counted = {p for p, net in reclass_net.items() if net != 0}
    for payment, invoice_id, amount in sorted(data.allocations):
        if payment not in counted:
            continue
        invoice = data.invoices.get(invoice_id)
        touched[payment].add(invoice_id)
        if invoice is None or invoice.total == 0:
            continue
        for tax_id, tax_amount in invoice.line_taxes:
            info = data.tax.get(tax_id)
            if info is None or not info.deferrable:
                continue
            part = prorate(amount, tax_amount, invoice.total)
            if part == 0:
                continue
            derived_by[(invoice_id, treatment(tax_id))].append(part)
            derived_payment[payment] += part
    bad_payments = {p for p in counted if derived_payment[p] != reclass_net[p]}
    unattributable = {inv for p in bad_payments for inv in touched[p]}

    # 3. one item set per carried invoice (zero-tax ones as a zero position)
    items: list[dict[str, Any]] = []
    credit_mismatch = 0
    for c in sorted(carried, key=lambda x: str(x.document_id)):
        invoice = data.invoices.get(c.invoice_key)
        if invoice is None:
            refuse("carried_invoice_not_in_legacy")
        tax_by: dict[str, tuple[Decimal, TaxInfo]] = {}
        for tax_id, tax_amount in invoice.lines:
            info = data.tax.get(tax_id) if tax_id else None
            if tax_amount != 0 and info is not None and info.deferrable:
                key = treatment(tax_id)
                tax_by[key] = (tax_by.get(key, (ZERO, info))[0] + tax_amount, info)
        credit = invoice_credit.get(c.invoice_key, ZERO)
        consistent = credit == sum((t for t, _ in tax_by.values()), ZERO)
        if not consistent:
            credit_mismatch += 1
        base = {
            "document_id": str(c.document_id),
            "total": fmt(invoice.total),
            "open_initial": fmt(c.open_initial),
            "legacy_ref": c.invoice_key,
            "finance_override": None,
        }
        if not tax_by:
            tax_by = {NO_TREATMENT: (ZERO, TaxInfo("", None, None))}
        for name in sorted(tax_by):
            tax, info = tax_by[name]
            attributable = consistent and c.invoice_key not in unattributable
            items.append(
                {
                    **base,
                    "treatment": name,
                    "tax": fmt(tax),
                    "journal_1455_credit": fmt(tax if consistent else credit),
                    "reclass": [fmt(x) for x in derived_by.get((c.invoice_key, name), [])] if attributable else None,
                    "deferral_account": info.deferral if tax != 0 else None,
                    "current_account": info.current if tax != 0 else None,
                }
            )

    # 4. the exact tie, from the raw lines, against the per-invoice remainders
    invoice_ids = set(invoice_credit) | {c.invoice_key for c in carried}
    derived_inv: dict[str, Decimal] = defaultdict(Decimal)
    for (invoice_id, _), parts in derived_by.items():
        derived_inv[invoice_id] += sum(parts, ZERO)
    remaining = {i: invoice_credit.get(i, ZERO) - derived_inv.get(i, ZERO) for i in invoice_ids | set(derived_inv)}
    carried_keys = {c.invoice_key for c in carried}
    carried_remaining = sum((v for k, v in remaining.items() if k in carried_keys), ZERO)
    other_invoice_remaining = sum((v for k, v in remaining.items() if k not in carried_keys), ZERO)
    reclass_diff = sum(derived_payment.values(), ZERO) - sum(reclass_net.values(), ZERO)
    other_net = sum(other.values(), ZERO)
    if carried_remaining + other_invoice_remaining + reclass_diff + other_net != gl_total:
        refuse("tie_failed")

    body = {"version": 1, "tenant_id": str(tenant_id), "items": items}
    loadable = sum(1 for i in items if _loadable(i))
    report = {
        "carried_invoices": len(carried),
        "items": len(items),
        "zero_tax_items": sum(1 for i in items if i["tax"] == fmt(ZERO)),
        "credit_mismatch_invoices": credit_mismatch,
        "payments_counted": len(counted),
        "payments_unattributable": len(bad_payments),
        "items_loadable": loadable,
        "items_e38": len(items) - loadable,
        "tie": {
            "gl_net_credit": fmt(gl_total),
            "carried_remaining": fmt(carried_remaining),
            "other_invoice_remaining": fmt(other_invoice_remaining),
            "reclass_diff": fmt(reclass_diff),
            "other_net": fmt(other_net),
            "other_by_source": {k: fmt(v) for k, v in sorted(other.items())},
        },
    }
    return body, report


def _loadable(item: dict[str, Any]) -> bool:
    parsed = {
        **item,
        "total": Decimal(item["total"]),
        "tax": Decimal(item["tax"]),
        "journal_1455_credit": Decimal(item["journal_1455_credit"]),
        "reclass": None if item["reclass"] is None else [Decimal(x) for x in item["reclass"]],
    }
    return vat_opening.derive(parsed)[1] is None


def apply_overrides(body: dict[str, Any], raw: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Finance's overrides: a separate, named input with evidence. An override that matches no item refuses."""
    doc = strict_json(raw)
    if not isinstance(doc, dict) or set(doc) != {"version", "named_by", "overrides"} or doc["version"] != 1:
        refuse("invalid_overrides")
    named_by = text(doc["named_by"], "invalid_overrides")
    index = {(i["document_id"], i["treatment"]): i for i in body["items"]}
    seen = set()
    for entry in doc["overrides"]:
        if not isinstance(entry, dict) or set(entry) != {
            "document_id",
            "treatment",
            "deferred",
            "evidence_sha256",
            "ref",
        }:
            refuse("invalid_overrides")
        key = (str(UUID(str(entry["document_id"]))), text(entry["treatment"], "invalid_overrides", 80))
        if key in seen or key not in index:
            refuse("override_unmatched")
        seen.add(key)
        index[key]["finance_override"] = {
            "deferred": fmt(money(entry["deferred"])),
            "evidence_sha256": sha_text(entry["evidence_sha256"], "invalid_overrides"),
            "ref": text(entry["ref"], "invalid_overrides"),
        }
    return body, {"named_by": named_by, "count": len(seen), "sha256": hashlib.sha256(raw).hexdigest()}


def seal(body: dict[str, Any]) -> tuple[bytes, str]:
    """Canonical, deterministic bytes of the items file and their sha256 (``vat-opening``'s input sha)."""
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    return data, hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- readers (read-only)


def read_carried(db, tenant_id: UUID, snapshot_sha256: str) -> list[Carried]:
    """The carried invoices: the #72 import receipt's receivable bases (structural link), one per imported open item."""
    import sqlalchemy as sa
    from dotmac_billing.models import BillingDocument, OpeningReceivableBasis

    if not db.scalar(sa.text("SELECT to_regclass('public.erp_ar_opening_admissions') IS NOT NULL")):
        refuse("ar_admissions_unavailable")
    snapshots = {
        row[0]
        for row in db.execute(
            sa.text("SELECT DISTINCT snapshot_sha256 FROM erp_ar_opening_admissions WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    }
    if snapshots != {snapshot_sha256}:
        refuse("snapshot_mismatch")
    receipts = [
        row[0]
        for row in db.execute(
            sa.text("SELECT billing_receipt_id FROM erp_ar_opening_admissions WHERE tenant_id = :t"), {"t": tenant_id}
        )
    ]
    bases = list(
        db.scalars(
            sa.select(OpeningReceivableBasis).where(
                OpeningReceivableBasis.tenant_id == tenant_id, OpeningReceivableBasis.receipt_id.in_(receipts)
            )
        )
    )
    imported = db.scalar(
        sa.select(sa.func.count())
        .select_from(BillingDocument)
        .where(BillingDocument.tenant_id == tenant_id, BillingDocument.authoring_mode == "imported_open_item")
    )
    if imported != len(bases):
        refuse("carried_count_mismatch")  # the #72 imported_open_item invoices and the receipt's bases must agree
    return [Carried(b.source_invoice_key, b.document_id, Decimal(b.initial_receivable), b.currency) for b in bases]


def read_legacy(conn, organization_id: UUID, cutoff: date, carried_keys: list[str]) -> Legacy:
    """Legacy reads on a read-only snapshot connection (the caller sets READ ONLY and ``row_security = off``)."""
    import sqlalchemy as sa

    org = str(organization_id)
    tax: dict[str, TaxInfo] = {}
    for tax_id, code, current, deferral in conn.execute(
        sa.text(
            "SELECT t.tax_code_id::text, t.tax_code, a.account_id::text, a.deferral_pair_account_id::text "
            "FROM tax.tax_code t LEFT JOIN gl.account a ON a.account_id = t.tax_collected_account_id "
            "WHERE t.organization_id = :o AND t.tax_type::text IN ('VAT', 'GST')"
        ),
        {"o": org},
    ):
        tax[tax_id] = TaxInfo(code, deferral, current)
    accounts = sorted({t.deferral for t in tax.values() if t.deferrable and t.deferral})
    gl = [
        GlLine(a, m, s, d, Decimal(c) - Decimal(db_), cur)
        for a, m, s, d, db_, c, cur in conn.execute(
            sa.text(
                "SELECT l.account_id::text, l.source_module, l.source_document_type, l.source_document_id::text, "
                "l.debit_amount, l.credit_amount, j.currency_code FROM gl.posted_ledger_line l "
                "JOIN gl.journal_entry j ON j.journal_entry_id = l.journal_entry_id "
                "WHERE l.organization_id = :o AND l.account_id = ANY(CAST(:a AS uuid[])) AND l.posting_date <= :c "
                "ORDER BY l.journal_entry_id, l.line_id"
            ),
            {"o": org, "a": accounts, "c": cutoff},
        )
    ]
    reclass_payments = sorted(
        {g.source_id for g in gl if g.source_module == "AR" and g.source_type == RECLASS_SOURCE and g.source_id}
    )
    allocations = [
        (p, i, Decimal(a))
        for p, i, a in conn.execute(
            sa.text(
                "SELECT payment_id::text, invoice_id::text, allocated_amount FROM ar.payment_allocation "
                "WHERE payment_id = ANY(CAST(:p AS uuid[]))"
            ),
            {"p": reclass_payments},
        )
    ]
    wanted = sorted(
        set(carried_keys)
        | {g.source_id for g in gl if g.source_module == "AR" and g.source_type in INVOICE_SOURCES and g.source_id}
        | {i for _, i, _ in allocations}
    )
    invoices: dict[str, Invoice] = {}
    for inv_id, number, total, currency in conn.execute(
        sa.text(
            "SELECT invoice_id::text, invoice_number, total_amount, currency_code FROM ar.invoice "
            "WHERE organization_id = :o AND invoice_id = ANY(CAST(:i AS uuid[]))"
        ),
        {"o": org, "i": wanted},
    ):
        invoices[inv_id] = Invoice(number, Decimal(total), currency)
    for inv_id, tax_id, tax_amount in conn.execute(
        sa.text(
            "SELECT invoice_id::text, tax_code_id::text, tax_amount FROM ar.invoice_line "
            "WHERE invoice_id = ANY(CAST(:i AS uuid[])) ORDER BY invoice_id, line_number"
        ),
        {"i": wanted},
    ):
        if inv_id in invoices:
            invoices[inv_id].lines.append((tax_id, Decimal(tax_amount or 0)))
    for inv_id, tax_id, tax_amount in conn.execute(
        sa.text(
            "SELECT l.invoice_id::text, lt.tax_code_id::text, lt.tax_amount FROM ar.invoice_line_tax lt "
            "JOIN ar.invoice_line l ON l.line_id = lt.line_id WHERE l.invoice_id = ANY(CAST(:i AS uuid[])) "
            "ORDER BY l.invoice_id, l.line_number, lt.sequence"
        ),
        {"i": wanted},
    ):
        if inv_id in invoices:
            invoices[inv_id].line_taxes.append((tax_id, Decimal(tax_amount)))
    return Legacy(tax, gl, invoices, allocations)


# --------------------------------------------------------------------------- CLI


def _create(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def main(argv: list[str] | None = None) -> int:
    from erp_v2.migration_ledger import cli_common

    parser = argparse.ArgumentParser(prog="erp_v2.billing_gl vat-extract")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--snapshot-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--treatment-map", type=Path)
    args = parser.parse_args(argv)
    try:
        tenant, org = UUID(args.tenant_id), UUID(args.organization_id)
        cutoff = date.fromisoformat(args.cutoff)
        sha_text(args.snapshot_sha256, "invalid_snapshot_sha")
        fd_out, fd_receipt = _create(args.out), _create(args.receipt)  # reserved before any read
    except Exception:
        print("vat-extract refused: arguments or output paths")
        return 2
    payload: dict[str, Any]
    code = 0
    try:
        import sqlalchemy as sa
        from sqlalchemy.orm import Session

        dest, legacy_engine = cli_common.engine(), cli_common.legacy_engine()
        with Session(dest) as db:
            db.execute(sa.text("SET TRANSACTION READ ONLY"))
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(tenant)})
            carried = read_carried(db, tenant, args.snapshot_sha256)
            db.rollback()
        with legacy_engine.connect() as conn:
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            conn.execute(sa.text("SET LOCAL row_security = off"))
            data = read_legacy(conn, org, cutoff, [c.invoice_key for c in carried])
            conn.rollback()
        mapping = {}
        if args.treatment_map:
            mapping = {str(k): str(v) for k, v in strict_json(args.treatment_map.read_bytes()).items()}
        body, report = derive(tenant, carried, data, mapping)
        overrides = None
        if args.overrides:
            body, overrides = apply_overrides(body, args.overrides.read_bytes())
        data_bytes, sha = seal(body)
        os.write(fd_out, data_bytes)
        payload = {
            "ready": True,
            "cutoff": cutoff.isoformat(),
            "snapshot_sha256": args.snapshot_sha256,
            "items_sha256": sha,
            "overrides": overrides,
            **report,
            "plan_preview": vat_opening.plan(vat_opening.parse(data_bytes), sha)["counts"],
        }
    except LedgerRefused as exc:
        payload, code = {"ready": False, "refusal": exc.code}, 2
    except Exception as exc:
        payload, code = {"ready": False, "refusal": "vat_extract_command_refused", "error_type": type(exc).__name__}, 2
    finally:
        os.close(fd_out)
    with os.fdopen(fd_receipt, "w") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print("vat-extract receipt written" if code == 0 else "vat-extract not applied; see private receipt")
    return code


if __name__ == "__main__":
    sys.exit(main())
