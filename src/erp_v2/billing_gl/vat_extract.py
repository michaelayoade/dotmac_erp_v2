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
from erp_v2.migration_ledger.common import LedgerRefused, canonical_sha, fmt, money, refuse, sha_text, strict_json, text
from erp_v2.migration_ledger.ledger_check import OPENING_OWNERS

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
    deferral_accounts: list[str] = field(default_factory=list)
    credit_note_ids: frozenset[str] = frozenset()  # ar.invoice ids of invoice_type CREDIT_NOTE
    fingerprint: dict[str, Any] = field(default_factory=dict)  # counts and max ids of the three legacy tables


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
    tenant_id: UUID,
    carried: list[Carried],
    data: Legacy,
    treatment_map: dict[str, str] | None = None,
    *,
    v2_opening_net_credit: Decimal,
    ap_positions: Decimal,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The sealed input body and the counts-only report. Pure: refuses rather than guesses.

    ``v2_opening_net_credit`` is ERP v2's own opening balance (B-9r, owner ``erp.opening``) of the deferral accounts at
    the cutoff; the legacy GL must equal it. ``ap_positions`` is the payables a6 remaining input deferral on those
    accounts; the AP-module lines must equal minus that. Both are INDEPENDENT of this derivation, so the tie can fail.
    """
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
    credit_note_lines: dict[str, Decimal] = defaultdict(Decimal)  # legacy posts a credit note's 1455 under its OWN id
    ar_other: dict[str, Decimal] = defaultdict(Decimal)  # AR lines no carried invoice or payment owns
    ar_other_count: dict[str, int] = defaultdict(int)
    ap_net = ZERO
    manual: dict[str, Decimal] = defaultdict(Decimal)
    gl_total = ZERO
    for line in data.gl:
        gl_total += line.net_credit
        if line.source_module == "AR" and line.source_id in data.credit_note_ids:
            credit_note_lines[line.source_id] += line.net_credit
        elif line.source_module == "AR" and line.source_type in INVOICE_SOURCES and line.source_id:
            invoice_credit[line.source_id] += line.net_credit
        elif line.source_module == "AR" and line.source_type == RECLASS_SOURCE and line.source_id:
            reclass_net[line.source_id] += -line.net_credit
        else:
            key = f"{line.source_module}/{line.source_type}"
            other[key] += line.net_credit
            if line.source_module == "AR":
                ar_other[key] += line.net_credit
                ar_other_count[key] += 1
            elif line.source_module == "AP":
                ap_net += line.net_credit
            else:
                manual[key] += line.net_credit

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
    # A credit note applied through a payment reduces SOME invoice, and legacy keeps no structural link to which one:
    # every invoice such a payment touches is unattributable (E38), never guessed.
    by_payment: dict[str, set[str]] = defaultdict(set)
    for payment, invoice_id, _ in data.allocations:
        by_payment[payment].add(invoice_id)
    credit_applied = {
        inv
        for invoices in by_payment.values()
        if invoices & data.credit_note_ids
        for inv in invoices
        if inv not in data.credit_note_ids
    }
    unattributable |= credit_applied

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

    # 4. the tie. The legacy GL must equal ERP v2's OWN opening balance of these accounts (an independent figure),
    #    the AP lines must equal the payables a6 positions, and everything no carried invoice owns is a NAMED residue.
    invoice_ids = set(invoice_credit) | {c.invoice_key for c in carried}
    derived_inv: dict[str, Decimal] = defaultdict(Decimal)
    for (invoice_id, _), parts in derived_by.items():
        derived_inv[invoice_id] += sum(parts, ZERO)
    remaining = {i: invoice_credit.get(i, ZERO) - derived_inv.get(i, ZERO) for i in invoice_ids | set(derived_inv)}
    carried_keys = {c.invoice_key for c in carried}
    carried_remaining = sum((v for k, v in remaining.items() if k in carried_keys), ZERO)
    non_carried = {k: v for k, v in remaining.items() if k not in carried_keys and v != 0}
    other_invoice_remaining = sum(non_carried.values(), ZERO)
    reclass_diff = sum(derived_payment.values(), ZERO) - sum(reclass_net.values(), ZERO)
    credit_note_net = sum(credit_note_lines.values(), ZERO)
    other_net = sum(other.values(), ZERO)
    if gl_total != v2_opening_net_credit:
        refuse("tie_failed")  # legacy GL at the cutoff vs ERP v2's opening balance of the deferral accounts
    if ap_net != -ap_positions:
        refuse("ap_positions_mismatch")  # AP-module lines vs the payables a6 remaining input deferral
    if carried_remaining + other_invoice_remaining + reclass_diff + credit_note_net + other_net != gl_total:
        refuse("tie_failed")  # internal consistency of the classification (kept as a guard)
    residue = {
        "ar_lines_without_carried_owner": {
            "count": sum(ar_other_count.values()),
            "amount": fmt(sum(ar_other.values(), ZERO)),
            "by_source": {k: fmt(v) for k, v in sorted(ar_other.items())},
        },
        "credit_notes": {"count": len(credit_note_lines), "amount": fmt(credit_note_net)},
        "non_carried_invoice_remainders": {"count": len(non_carried), "amount": fmt(other_invoice_remaining)},
        "reclass_mismatch": {"payments": len(bad_payments), "amount": fmt(reclass_diff)},
        "manual_non_ar_non_ap": {
            "amount": fmt(sum(manual.values(), ZERO)),
            "by_source": {k: fmt(v) for k, v in sorted(manual.items())},
        },
        "credit_note_applied_invoices": len(credit_applied & carried_keys),
    }
    residue_sha = canonical_sha(residue)

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
            "credit_note_net": fmt(credit_note_net),
            "other_net": fmt(other_net),
            "v2_opening_net_credit": fmt(v2_opening_net_credit),
            "ap_positions": fmt(ap_positions),
        },
        "residue": residue,
        "residue_sha256": residue_sha,
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
        if _loadable(index[key]):
            refuse("override_on_loadable_item")
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


def read_v2_opening(db, tenant_id: UUID, accounts: list[str]) -> Decimal:
    """ERP v2's opening balance (the B-9r opening and IAS 8 journals) of the deferral accounts: net credit."""
    import sqlalchemy as sa
    from dotmac_accounting.models import PostedLedgerLine

    total = db.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(PostedLedgerLine.credit - PostedLedgerLine.debit), 0)).where(
            PostedLedgerLine.tenant_id == tenant_id,
            PostedLedgerLine.account_id.in_([UUID(a) for a in accounts]),
            PostedLedgerLine.source_owner.in_(OPENING_OWNERS),
        )
    )
    return Decimal(total)


def read_ap_positions(db, tenant_id: UUID, accounts: list[str]) -> Decimal:
    """Payables a6 remaining input deferral on the deferral accounts (``historical_tax_deferrals``), or 0 when the
    payables release that carries it is not installed (the caller then also requires there to be no AP-module line).

    The destination connection is the migration role, which BYPASSES row-level security, so the tenant filter is
    explicit and the read refuses if more than one tenant would be summed."""
    import sqlalchemy as sa

    if not db.scalar(sa.text("SELECT to_regclass('mod_payables.historical_tax_deferrals') IS NOT NULL")):
        return ZERO
    rows = db.execute(
        sa.text(
            "SELECT tenant_id, coalesce(sum(remaining), 0) FROM mod_payables.historical_tax_deferrals "
            "WHERE tenant_id = :t AND deferral_account_ref = ANY(:a) GROUP BY tenant_id"
        ),
        {"t": tenant_id, "a": accounts},
    ).all()
    if len(rows) > 1 or any(row[0] != tenant_id for row in rows):
        refuse("ap_positions_multi_tenant")
    return Decimal(rows[0][1]) if rows else ZERO


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
    credit_note_ids = frozenset(
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT invoice_id::text FROM ar.invoice "
                "WHERE organization_id = :o AND invoice_type::text = 'CREDIT_NOTE'"
            ),
            {"o": org},
        )
    )
    reclass_payments = sorted(
        {g.source_id for g in gl if g.source_module == "AR" and g.source_type == RECLASS_SOURCE and g.source_id}
        | {
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT DISTINCT payment_id::text FROM ar.payment_allocation "
                    "WHERE invoice_id = ANY(CAST(:c AS uuid[]))"
                ),
                {"c": sorted(credit_note_ids)},
            )
        }
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
        | credit_note_ids
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
    fingerprint: dict[str, Any] = {}
    for name, sql in (
        (
            "gl_posted_ledger_line",
            "SELECT count(*), max(line_id::text) FROM gl.posted_ledger_line WHERE organization_id = :o",
        ),
        ("ar_invoice", "SELECT count(*), max(invoice_id::text) FROM ar.invoice WHERE organization_id = :o"),
        (
            "ar_payment_allocation",
            "SELECT count(*), max(allocation_id::text) FROM ar.payment_allocation a WHERE EXISTS "
            "(SELECT 1 FROM ar.invoice i WHERE i.invoice_id = a.invoice_id AND i.organization_id = :o)",
        ),
    ):
        count, max_id = conn.execute(sa.text(sql), {"o": org}).one()
        fingerprint[name] = {"count": int(count), "max_id": max_id}
    return Legacy(tax, gl, invoices, allocations, accounts, credit_note_ids, fingerprint)


# --------------------------------------------------------------------------- CLI


def residue_gate(report: dict[str, Any], acknowledge: str | None, operator_ref: str | None) -> str | None:
    """The residue-acknowledgement gate: None when the run may write its items file, else the refusal code.

    A non-zero named residue needs Finance's acknowledgement: the exact residue sha and the acknowledging user."""
    if not _has_residue(report["residue"]):
        return None
    if acknowledge != report["residue_sha256"]:
        return "residue_unacknowledged"
    if not operator_ref or not operator_ref.strip():
        return "operator_ref_required"
    return None


class _Acknowledge(Exception):
    """Control flow: the residue needs Finance's acknowledgement before any items file is written."""


def _has_residue(residue: dict[str, Any]) -> bool:
    def nonzero(value: Any) -> bool:
        if isinstance(value, dict):
            return any(nonzero(v) for v in value.values())
        if isinstance(value, str):
            try:
                return Decimal(value) != 0
            except Exception:
                return False
        return bool(value)

    return nonzero(residue)


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
    parser.add_argument(
        "--acknowledge-residue",
        help="the residue_sha256 of a previous receipt: Finance's acknowledgement of the named E38 residue",
    )
    parser.add_argument("--operator-ref", help="the Finance user acknowledging the residue (recorded in the receipt)")
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
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
            carried = read_carried(db, tenant, args.snapshot_sha256)
            db.rollback()
        with legacy_engine.connect() as conn:
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            conn.execute(sa.text("SET LOCAL row_security = off"))
            data = read_legacy(conn, org, cutoff, [c.invoice_key for c in carried])
            conn.rollback()
        mapping: dict[str, str] = {}
        map_sha = None
        if args.treatment_map:
            raw_map = args.treatment_map.read_bytes()
            map_sha = hashlib.sha256(raw_map).hexdigest()
            mapping = {str(k): str(v) for k, v in strict_json(raw_map).items()}
        with Session(dest) as db:
            db.execute(sa.text("SET TRANSACTION READ ONLY"))
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
            v2_opening = read_v2_opening(db, tenant, data.deferral_accounts)
            ap_positions = read_ap_positions(db, tenant, data.deferral_accounts)
            db.rollback()
        body, report = derive(
            tenant, carried, data, mapping, v2_opening_net_credit=v2_opening, ap_positions=ap_positions
        )
        base = {
            "cutoff": cutoff.isoformat(),
            "snapshot_sha256": args.snapshot_sha256,
            "treatment_map_sha256": map_sha,
            "legacy_fingerprint": data.fingerprint,
            "legacy_fingerprint_sha256": canonical_sha(data.fingerprint),
            **report,
        }
        refusal = residue_gate(report, args.acknowledge_residue, args.operator_ref)
        if refusal:
            payload, code = {"ready": False, "refusal": refusal, **base}, 2
            raise _Acknowledge
        overrides = None
        if args.overrides:
            body, overrides = apply_overrides(body, args.overrides.read_bytes())
        data_bytes, sha = seal(body)
        with Session(dest) as db:  # the preview runs the same two gates as the real plan, from the destination
            db.execute(sa.text("SET TRANSACTION READ ONLY"))
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
            preview = vat_opening.plan(
                vat_opening.parse(data_bytes),
                sha,
                check=vat_opening.billing_check(db, tenant),
                treatments=vat_opening.configured_treatments(db, tenant),
            )["counts"]
            db.rollback()
        os.write(fd_out, data_bytes)
        payload = {
            "ready": True,
            **base,
            "residue_acknowledged": _has_residue(report["residue"]),
            "acknowledged_by": args.operator_ref if _has_residue(report["residue"]) else None,
            "items_sha256": sha,
            "overrides": overrides,
            "plan_preview": preview,
        }
    except _Acknowledge:
        pass  # the receipt (with the residue and its sha) is already set; no items file is written
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
