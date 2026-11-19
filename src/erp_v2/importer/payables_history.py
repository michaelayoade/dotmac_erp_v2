"""Trusted, SELECT-only proof for the bounded unpaid historical AP admission.

The operator supplies a read-only restored snapshot Connection and its held
manifest. Database names and read-only mode are checked, but a manifest digest
alone cannot cryptographically prove the bytes used to restore a database.
The destination Session is supplied by the owner, sharing its invoice lock and
transaction. This verifier never constructs connections, writes, or commits.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.semantic_encoding import ABSENT, digest_of, encode_fields, encode_ordered
from dotmac_payables import HistoricalUnpaidInvoiceCommand, HistoricalUnpaidInvoiceEvidence
from sqlalchemy.orm import Session

from erp_v2.importer import admission_waivers, tax_accounts
from erp_v2.importer.cutoff_evidence import journal_proof
from erp_v2.importer.payables_reconciliation import _money, _reflect, _sum
from erp_v2.importer.snapshot import SnapshotManifest
from erp_v2.legacy_ap.transform_manifest import InvoiceTransform, TransformManifest

ADMISSION = frozenset({"admit_residual", "admit_with_waivers"})
STATUS_WAIVERS = frozenset({"inconsistent_status_pair", "legacy_journal_link_unposted"})


class HistoricalEvidenceError(ValueError):
    """A named fail-closed evidence failure; contains no business row contents."""

    def __init__(self, reason: str, flag: str | None = None):
        self.reason = reason
        self.flag = flag  # e.g. the A3 reason (deferral_amount_mismatch); evidence only, never the refusal key
        super().__init__(reason)


NUMBERS = frozenset(
    {
        "exchange_rate",
        "subtotal",
        "tax_amount",
        "total_amount",
        "amount_paid",
        "functional_currency_amount",
        "withholding_tax_amount",
        "prepayment_applied",
        "stamp_duty_amount",
        "allocated_amount",
        "discount_taken",
        "exchange_difference",
        "gross_amount",
        "amount",
        "quantity",
        "unit_price",
        "line_amount",
        "total_debit",
        "total_credit",
        "total_debit_functional",
        "total_credit_functional",
        "debit_amount",
        "credit_amount",
        "debit_amount_functional",
        "credit_amount_functional",
        "original_debit_amount",
        "original_credit_amount",
        "debit",
        "credit",
        "debit_functional",
        "credit_functional",
        "original_debit",
        "original_credit",
    }
)
DIMENSIONS = (
    ("BUSINESS_UNIT", "business_unit_id"),
    ("COST_CENTER", "cost_center_id"),
    ("PROJECT", "project_id"),
    ("SEGMENT", "segment_id"),
)


# These projections are the frozen v1 decision shape. Adding a database column
# must not silently re-key permanent historical evidence. A new material fact
# requires an explicit domain/version cutover and new reviewed commands.
SOURCE_HEADER = """organization_id invoice_id supplier_id invoice_number supplier_invoice_number invoice_type status
posting_status invoice_date received_date due_date currency_code exchange_rate subtotal tax_amount total_amount
amount_paid functional_currency_amount ap_control_account_id journal_entry_id posted_at withholding_tax_amount
withholding_tax_code_id prepayment_applied is_prepayment stamp_duty_amount submitted_by_user_id submitted_at
approved_by_user_id approved_at approval_request_id created_at""".split()
SOURCE_SUPPLIER = "supplier_id organization_id legal_name trading_name".split()
SOURCE_ORGANIZATION = "organization_id functional_currency_code".split()
SOURCE_INVOICE_LINE = """line_id invoice_id line_number description quantity unit_price line_amount tax_amount
tax_code_id expense_account_id cost_center_id project_id segment_id po_line_id goods_receipt_line_id
created_at""".split()
DESTINATION_HEADER = """id tenant_id number supplier_ref supplier_name_snapshot supplier_document_number invoice_date
received_date currency_code exchange_rate liability_account_ref subtotal tax_amount total_amount payment_schedule
procurement_ref receipt_evidence_fingerprint submitted_by submitted_at approved_by approved_at
approval_reference request_fingerprint created_at""".split()
DESTINATION_INVOICE_LINE = """id tenant_id invoice_id line_number description quantity unit_price line_amount tax_amount
posting_account_ref tax_account_ref dimension_refs created_at""".split()
SOURCE_JOURNAL = """journal_entry_id organization_id journal_number journal_type status entry_date posting_date
fiscal_period_id description reference currency_code exchange_rate total_debit total_credit total_debit_functional
total_credit_functional source_module source_document_type source_document_id is_reversal reversed_journal_id
reversal_journal_id posted_by_user_id posted_at created_at""".split()
DESTINATION_JOURNAL = """id tenant_id number kind status entry_date posting_date fiscal_period_id description reference
currency_code exchange_rate total_debit total_credit total_debit_functional total_credit_functional source_owner
source_document_kind source_document_id source_version source_fingerprint request_fingerprint approval_reference
posted_by posted_at reverses_journal_id reversal_journal_id created_at""".split()
SOURCE_JOURNAL_LINE = """line_id journal_entry_id line_number account_id description debit_amount credit_amount
debit_amount_functional credit_amount_functional currency_code exchange_rate business_unit_id cost_center_id
project_id segment_id created_at""".split()
DESTINATION_JOURNAL_LINE = """id tenant_id journal_id line_number account_id description debit credit debit_functional
credit_functional created_at""".split()
SOURCE_LEDGER = """ledger_line_id organization_id journal_entry_id journal_line_id account_id account_code
fiscal_period_id
entry_date posting_date description journal_reference debit_amount credit_amount original_currency_code
original_debit_amount original_credit_amount exchange_rate source_module source_document_type source_document_id
posted_by_user_id posted_at business_unit_id cost_center_id project_id segment_id""".split()
DESTINATION_LEDGER = """id tenant_id journal_id journal_line_id account_id account_code fiscal_period_id journal_number
entry_date posting_date description debit credit original_debit original_credit currency_code exchange_rate source_owner
source_document_kind source_document_id source_version source_fingerprint posted_by posted_at""".split()


# The held legacy PostgreSQL snapshot contains AP and AR tables with the same
# allocation name. Bind the source contract to its evidenced namespaces.
SOURCE_SCHEMAS = {
    "organization": "core_org",
    "supplier": "ap",
    "supplier_invoice": "ap",
    "supplier_invoice_line": "ap",
    "payment_allocation": "ap",
    "supplier_payment": "ap",
    "journal_entry": "gl",
    "journal_entry_line": "gl",
    "posted_ledger_line": "gl",
    "tax_code": "tax",
    "account": "gl",
}


# The importer stamps the resolved, schema-qualified source table (for example
# ``legacy:gl.journal_entry:<id>``), so derive the expected prefix from the same
# source contract the verifier reads the journal with. Never accept other schemas.
SOURCE_JOURNAL_REFERENCE = f"legacy:{SOURCE_SCHEMAS['journal_entry']}.journal_entry:"


READ_FIELDS = {
    "supplier_invoice": SOURCE_HEADER,
    "supplier": SOURCE_SUPPLIER,
    "organization": SOURCE_ORGANIZATION,
    "supplier_invoice_line": SOURCE_INVOICE_LINE,
    "supplier_invoices": DESTINATION_HEADER,
    "supplier_invoice_lines": DESTINATION_INVOICE_LINE,
    "journal_entry": SOURCE_JOURNAL,
    "journal_entries": DESTINATION_JOURNAL,
    "journal_entry_line": SOURCE_JOURNAL_LINE,
    "journal_lines": DESTINATION_JOURNAL_LINE,
    "posted_ledger_line": SOURCE_LEDGER,
    "tax_code": ["tax_code_id", "organization_id", "tax_type", "tax_paid_account_id"],
    "account": ["account_id", "deferral_pair_account_id"],
    "posted_ledger_lines": DESTINATION_LEDGER,
    "payment_allocation": ["invoice_id"],
    "journal_line_dimensions": "id tenant_id journal_line_id dimension_id dimension_value_id".split(),
    "posted_ledger_dimensions": (
        "id tenant_id ledger_line_id dimension_id dimension_code dimension_value_id value_code"
    ).split(),
    "accounting_dimensions": ["id", "tenant_id", "code"],
    "accounting_dimension_values": ["id", "tenant_id", "dimension_id", "code"],
}


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise HistoricalEvidenceError(reason)


@dataclass(frozen=True)
class ReviewFacts:
    """The Finance decision row (``erp_admission_reviews``) the verifiers honour, read from the destination."""

    review_id: str
    decision: str
    waived: tuple[str, ...]
    evidence_sha: str
    decision_file_sha: str
    ias8_account_id: str | None = None
    legacy_journal_id: str | None = None


class WaiverGate:
    """Per-evidence-call gate for waivable checks (reviewed admission, D-M3).

    ``require`` behaves exactly as ``_require`` unless the failing check is named in the invoice's Finance
    decision (one name, from the closed allowlist), in which case the check is skipped and the name is recorded
    as used. ``finish`` refuses a decision that waived a check that did not fail (a stale decision). With no
    review row the gate is inert and every verifier behaves as before.
    """

    def __init__(self, review: ReviewFacts | None = None, transform: dict[str, str] | None = None):
        """``transform`` maps a waivable check name to the id of the approved importer transform that fixes that
        failure (read from the transform manifest by the caller). A transform-fixed failure needs no waiver and
        does not trip ``waiver_not_needed``; transforms and waivers are kept distinct in the evidence."""
        self.review = review
        self.waived = frozenset(review.waived) if review else frozenset()
        self.transform = dict(transform or {})
        self.used: set[str] = set()
        self.transformed: dict[str, str] = {}

    def require(self, ok: bool, reason: str, waiver: str | tuple[str, ...] | None = None, flag: str | None = None):
        if ok:
            return
        names = (waiver,) if isinstance(waiver, str) else (waiver or ())
        for name in names:
            if name in self.transform and reason in admission_waivers.WAIVABLE_CHECKS.get(name, ()):
                self.transformed[name] = self.transform[name]
                return
        for name in names:
            if name in self.waived and reason in admission_waivers.WAIVABLE_CHECKS.get(name, ()):
                self.used.add(name)
                return
        raise HistoricalEvidenceError(reason, flag=flag)

    def waived_used(self, name: str) -> bool:
        return name in self.used

    def finish(self) -> None:
        _require(self.used == self.waived, "waiver_not_needed")

    def facts(self) -> dict:
        """Fingerprint keys; absent when nothing is waived, so existing fingerprints are unchanged."""
        out: dict = {}
        if self.transformed:
            out["transforms"] = dict(sorted(self.transformed.items()))
        if self.waived and self.review is not None:
            out["review"] = dict(
                waived=sorted(self.waived),
                decision=self.review.decision,
                evidence_sha=self.review.evidence_sha,
                decision_file_sha=self.review.decision_file_sha,
            )
        return out


def load_review(
    dest: sa.Connection, tenant: UUID, invoice_id: UUID, snapshot_sha256: str, *, allowed: frozenset[str]
) -> ReviewFacts | None:
    """The Finance decision for this invoice, or None. Fail closed: a row that is not usable here refuses.

    A missing table reads as no decision (the verifiers then stay at their strict default).
    """
    from erp_v2.legacy_ap.models import AdmissionReview

    table = AdmissionReview.__table__
    if not sa.inspect(dest).has_table(table.name):
        return None
    rows = (
        dest.execute(sa.select(table).where(table.c.tenant_id == tenant, table.c.legacy_invoice_id == invoice_id))
        .mappings()
        .all()
    )
    if not rows:
        return None
    _require(len(rows) == 1, "review_duplicate")
    row = rows[0]
    _require(str(row["tenant_id"]) == str(tenant), "review_tenant")
    _require(row["snapshot_sha256"] == snapshot_sha256, "review_snapshot_mismatch")
    _require(row["decision"] in allowed, "review_decision_not_admission")
    waived = row["waived_checks"]
    if isinstance(waived, str):
        waived = json.loads(waived)
    _require(
        isinstance(waived, list)
        and len(waived) <= admission_waivers.MAX_WAIVERS_PER_INVOICE
        and all(name in admission_waivers.WAIVABLE_CHECKS for name in waived)
        and len(set(waived)) == len(waived)
        and (row["decision"] == "admit_with_waivers") == bool(waived),
        "review_waivers_invalid",
    )
    return ReviewFacts(
        review_id=str(row["id"]),
        decision=row["decision"],
        waived=tuple(waived),
        evidence_sha=row["evidence_sha"],
        decision_file_sha=row["decision_file_sha"],
        ias8_account_id=None if row["ias8_account_id"] is None else str(row["ias8_account_id"]),
        legacy_journal_id=None if row["legacy_journal_id"] is None else str(row["legacy_journal_id"]),
    )


def admission_notes(src: sa.Connection, invoice_id: UUID, review: ReviewFacts | None) -> dict:
    """Readable admission evidence for receipts and audit (not only the fingerprint): the legacy header status
    pair, the waived check name, and whether the admission needs a correcting journal (E08)."""
    if review is None:
        return {}
    header = _one(_read(src, "supplier_invoice", key="invoice_id", value=invoice_id), "source_invoice_missing")
    return dict(
        review_id=review.review_id,
        review_decision=review.decision,
        waived_checks=list(review.waived),
        legacy_header_status=f"{header['status']}/{header['posting_status']}",
        decision_file_sha=review.decision_file_sha,
        evidence_sha=review.evidence_sha,
        requires_correcting_journal=bool(set(review.waived) & admission_waivers.REQUIRES_CORRECTING_JOURNAL),
    )


def _allocations_without_control_effect(src: sa.Connection, invoice_id: UUID, control: object) -> None:
    """Never waivable: waiving ``source_allocations`` must not hide a payment that posted to the AP control
    account. Positive proof is required for EVERY allocation: its payment row exists and is either not CLEARED
    (the only status that posts to the GL), or is CLEARED, has a journal link, the journal's posted ledger lines
    are found by that link (never by ``source_document_id``, which legacy leaves empty on many journals) and none
    debits the control account. Anything else is ``allocation_payment_gl_unproven``."""
    try:
        table = _reflect(
            src,
            "payment_allocation",
            SOURCE_SCHEMAS["payment_allocation"] if src.dialect.name == "postgresql" else None,
        )
        _require("payment_id" in table.c, "allocation_payment_gl_unproven")
        allocations = _read(src, "payment_allocation", key="invoice_id", value=invoice_id, fields=["payment_id"])
        for alloc in allocations:
            _require(alloc["payment_id"] is not None, "allocation_payment_gl_unproven")
            payments = _read(
                src,
                "supplier_payment",
                key="payment_id",
                value=alloc["payment_id"],
                fields=["payment_id", "status", "journal_entry_id"],
            )
            _require(len(payments) == 1, "allocation_payment_gl_unproven")
            payment = payments[0]
            if payment["status"] != "CLEARED":
                continue
            _require(payment["journal_entry_id"] is not None, "allocation_payment_gl_unproven")
            lines = _read(src, "posted_ledger_line", key="journal_entry_id", value=payment["journal_entry_id"])
            _require(bool(lines), "allocation_payment_gl_unproven")
            _require(
                not any(_equal(line["account_id"], control) and line["debit_amount"] != 0 for line in lines),
                "allocation_payment_gl_effect",
            )
    except (sa.exc.NoSuchTableError, sa.exc.NoSuchColumnError, KeyError, HistoricalEvidenceError) as exc:
        if isinstance(exc, HistoricalEvidenceError) and exc.reason in (
            "allocation_payment_gl_effect",
            "allocation_payment_gl_unproven",
        ):
            raise
        raise HistoricalEvidenceError("allocation_payment_gl_unproven") from exc


def _journal_not_shared(src: sa.Connection, dest: sa.Connection, journal_id: object, invoice_id: object) -> None:
    """Never waivable (``source_journal_shared``): one legacy journal never backs two invoices. Refuses when
    another legacy invoice references the journal, or when a historical invoice or settlement receipt of
    another document already names it."""
    others = [
        r
        for r in _read(src, "supplier_invoice", key="journal_entry_id", value=journal_id)
        if not _equal(r["invoice_id"], invoice_id)
    ]
    _require(not others, "source_journal_shared")
    jid = _id(journal_id)
    inspector = sa.inspect(dest)
    for table, own in (("historical_invoice_receipts", "invoice_id"), ("historical_settlement_records", None)):
        if not inspector.has_table(table, schema="mod_payables"):
            continue
        statement = (
            f"SELECT count(*) FROM mod_payables.{table} WHERE (source_journal_ref = :j OR destination_journal_ref = :j)"  # noqa: S608
        )
        params: dict = {"j": jid}
        if own:
            statement += f" AND {own} <> :i"
            params["i"] = UUID(str(invoice_id)) if dest.dialect.name == "postgresql" else str(invoice_id)
        _require(dest.execute(sa.text(statement), params).scalar_one() == 0, "source_journal_shared")


def _status_waiver(header: dict, *, settled: bool) -> str:
    """The vocabulary name of a header status failure: a legacy journal link on a header that is not POSTED is
    ``legacy_journal_link_unposted``; any other status/posting-status disagreement is ``inconsistent_status_pair``."""
    if header["posting_status"] != "POSTED" and header["journal_entry_id"] is not None:
        return "legacy_journal_link_unposted"
    return "inconsistent_status_pair"


def _id(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


def line_facts(src: sa.Connection, lines: list[dict]) -> tuple[list[tax_accounts.LineFacts], list[dict]]:
    """The tax-rule facts of each source line (tax code and account rows read from the snapshot) and the evidence
    entries that enter the fingerprint. A missing referenced row is a named refusal."""

    def one(name: str, key: str, value: object, reason: str) -> dict:
        return _one(_read(src, name, key=key, value=value), reason)

    facts, evidence = [], []
    for line in lines:
        code = None
        if line["tax_code_id"] is not None and line["tax_amount"] != 0:
            row = one("tax_code", "tax_code_id", line["tax_code_id"], "tax_code_row_missing")
            paid = deferral = None
            if row["tax_paid_account_id"] is not None:
                paid = one("account", "account_id", row["tax_paid_account_id"], "tax_paid_account_row_missing")
                if paid["deferral_pair_account_id"] is not None:
                    one("account", "account_id", paid["deferral_pair_account_id"], "tax_deferral_account_row_missing")
                    deferral = paid["deferral_pair_account_id"]
            code = tax_accounts.TaxCodeFacts(
                row["organization_id"], row["tax_type"], row["tax_paid_account_id"], deferral
            )
            evidence.append(
                dict(
                    line_id=_id(line["line_id"]),
                    tax_code_id=_id(row["tax_code_id"]),
                    organization_id=_id(row["organization_id"]),
                    tax_type=str(row["tax_type"]),
                    paid_account_id=None if code.paid_account_id is None else _id(code.paid_account_id),
                    deferral_account_id=None if deferral is None else _id(deferral),
                )
            )
        facts.append(
            tax_accounts.LineFacts(
                line["line_id"], line["line_amount"], line["tax_amount"], line["expense_account_id"], code
            )
        )
    return facts, evidence


def rederive_transform(
    verifier,
    tenant: UUID,
    invoice_id: UUID,
    header: dict,
):
    """Re-derive the importer's transform for this invoice from the SNAPSHOT (never from the manifest file) and
    require manifest == re-derived. Returns the verified ``InvoiceTransform`` (None: no rule changed anything).

    The declared fallback account is bound in the plan (``verifier.fallback_expense_account``) and must be the one
    the manifest was built with. Each rule's precondition (WITHHOLDING-type tax only, rounding under a cent, the
    declared fallback, the legacy GL mapping, items link-only) is exactly the pure rule re-run here, so a forged
    or edited manifest cannot launder a destination that the rules would not have produced.
    """
    from erp_v2.importer import transforms as rules
    from erp_v2.legacy_ap.transform_manifest import InvoiceTransform

    tm = verifier.transform_manifest
    if tm is None:
        return None
    src = verifier.source
    _require(tm.snapshot_sha256 == verifier.manifest.snapshot_sha256, "transform_manifest_snapshot")
    declared = getattr(verifier, "fallback_expense_account", None)
    _require(bool(declared) and tax_accounts.canonical(declared) == tm.fallback_account, "transform_fallback_account")
    lines = _read(src, "supplier_invoice_line", key="invoice_id", value=invoice_id)
    facts, _evidence = line_facts(src, lines)
    outcome, _tax_evidence = invoice_tax(
        src,
        tenant,
        header["journal_entry_id"],
        lines,
        invoice_id,
        header["ap_control_account_id"],
        header["subtotal"],
        header["total_amount"],
    )
    accounts = {
        tax_accounts.canonical(r["account_id"]) or "": rules.legacy_account_facts(r)
        for r in _read(src, "account", fields=["account_id", "account_code", "is_active", "is_posting_allowed"])
    }
    items = {
        tax_accounts.canonical(r["line_id"]) or "": str(r["item_id"])
        for r in _read(src, "supplier_invoice_line", key="invoice_id", value=invoice_id, fields=["line_id", "item_id"])
        if r["item_id"] is not None
    }
    derived = rules.transform_invoice(
        rules.InvoiceFacts(
            tax_accounts.canonical(invoice_id) or "",
            header["organization_id"],
            str(header["status"] or ""),
            header["subtotal"],
            header["tax_amount"],
            header["total_amount"],
            tuple(facts),
            outcome,
            items,
        ),
        tm.fallback_account,
        accounts,
        tm.unposted_wht,
    )
    _require(derived.lands, "transform_invoice_not_landable")
    expected = InvoiceTransform.from_outcome(derived)
    _require(expected == tm.for_invoice(invoice_id), "transform_manifest_mismatch")
    return expected


def invoice_tax(
    src: sa.Connection,
    organization: object,
    journal_id: object,
    lines: list[dict],
    invoice_id: object = None,
    control_account: object = None,
    header_subtotal: object = None,
    header_total: object = None,
) -> tuple[tax_accounts.InvoiceTax, list[dict]]:
    """Tax-account classification of one invoice from the snapshot (the importer's rule, shared).

    ``journal_id`` is the legacy invoice journal, or None for an invoice with no GL footprint. The
    legacy-posted regime follows the posted ledger debits; a missing tax code or account row is a
    named refusal. Also returns the tax facts that enter the evidence fingerprint.
    """

    facts, evidence = line_facts(src, lines)
    # Every tax code's paid/deferral accounts, not only this invoice's codes: a debit on another code's
    # VAT account must keep the invoice out of A1 and A2.
    every_tax_account: list = []
    if any(line["tax_amount"] != 0 for line in lines):
        pairs = {_id(a["account_id"]): a["deferral_pair_account_id"] for a in _read(src, "account")}
        for code_row in _read(src, "tax_code"):
            paid_id = code_row["tax_paid_account_id"]
            if paid_id is not None:
                every_tax_account += [paid_id, pairs.get(_id(paid_id))]
    debits = None
    net: dict[str, Decimal] | None = None
    if journal_id is not None:
        ledger = _read(src, "posted_ledger_line", key="journal_entry_id", value=journal_id)
        if ledger:
            debits, net = {}, {}
            for row in ledger:
                key = _id(row["account_id"])
                debits[key] = debits.get(key, Decimal(0)) + row["debit_amount"]
                net[key] = net.get(key, Decimal(0)) + row["debit_amount"] - (row["credit_amount"] or Decimal(0))
    # Legacy VAT-deferral journals of the invoice (conversion spec section 9): linked by document type, the
    # invoice id, module AP; POSTED ones are read from their posted ledger lines, others are counted and ignored.
    deferral = None
    if invoice_id is not None:
        journals, ids, not_posted = [], [], 0
        for header in _read(src, "journal_entry", key="source_document_id", value=invoice_id):
            if (
                str(header["source_document_type"]) != tax_accounts.DEFERRAL_DOCUMENT_TYPE
                or str(header["source_module"]) != "AP"
            ):
                continue
            if str(header["status"]).strip().upper() != "POSTED":
                not_posted += 1
                continue
            jnet: dict[str, Decimal] = {}
            for row in _read(src, "posted_ledger_line", key="journal_entry_id", value=header["journal_entry_id"]):
                key = _id(row["account_id"])
                jnet[key] = jnet.get(key, Decimal(0)) + row["debit_amount"] - (row["credit_amount"] or Decimal(0))
            journals.append(jnet)
            ids.append(_id(header["journal_entry_id"]))
        deferral = tax_accounts.DeferralFacts(tuple(journals), not_posted, tuple(ids))
    try:
        outcome = tax_accounts.classify_invoice(
            organization,
            facts,
            debits,
            every_tax_account,
            invoice_net=net,
            deferral=deferral,
            control_account=control_account,
            header_subtotal=header_subtotal,
            header_total=header_total,
        )
    except tax_accounts.TaxAccountMappingError as exc:
        raise HistoricalEvidenceError(str(exc)) from exc
    return outcome, sorted(evidence, key=lambda item: item["line_id"])


def _deferral_evidence(outcome: tax_accounts.InvoiceTax) -> dict:
    """Fingerprint keys for the deferral-journal rule; absent (so existing fingerprints are stable) when the invoice
    has no deferral journal."""

    if not (outcome.deferral_journals or outcome.deferral_not_posted):
        return {}
    return dict(
        deferral_journal_ids=sorted(outcome.deferral_journal_ids),
        deferral_journals=outcome.deferral_journals,
        deferral_not_posted=outcome.deferral_not_posted,
        a3_reason=outcome.reason,
    )


def transform_facts(found: InvoiceTransform, manifest: TransformManifest) -> dict:
    """Fingerprint evidence of the importer transforms that explain this invoice (separate from ``review``)."""
    return dict(
        manifest_sha256=manifest.sha256,
        rules=sorted(found.rules),
        header=sorted((k, format(v, "f")) for k, v in found.header.items()),
        lines=sorted(
            (
                k,
                v["posting_account_ref"],
                v["tax_account_ref"],
                format(v["line_amount"], "f"),
                format(v["tax_amount"], "f"),
            )
            for k, v in found.lines.items()
        ),
        item_lines=sorted(found.item_lines),
    )


def require_line_tax_account(
    outcome: tax_accounts.InvoiceTax, line_id: object, imported: dict, transform=None
) -> tax_accounts.LineTax:
    """The destination ref must equal the derived ref exactly (untaxed lines: NULL). A line the importer transformed
    (``transform``: an ``InvoiceTransform`` of the transform manifest) must carry the ref the manifest names."""

    derived = outcome.lines[tax_accounts.canonical(line_id)]
    actual = imported["tax_account_ref"]
    explained, landed = transform.landed_tax_ref(line_id) if transform is not None else (False, None)
    expected = landed if explained else derived.ref
    _require(
        (actual is None and expected is None)
        or (actual is not None and expected is not None and _id(actual) == expected),
        "invoice_line_tax_account",
    )
    return derived


def _equal(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is b
    return _id(a) == _id(b) if isinstance(a, (str, UUID)) and isinstance(b, (str, UUID)) else a == b


def _amount(value: object, reason: str) -> Decimal:
    try:
        return _money(value)
    except ValueError as exc:
        raise HistoricalEvidenceError(reason) from exc


def _at_scale(value: Decimal, places: int) -> bool:
    parts = value.as_tuple()
    extra = -int(parts.exponent) - places
    return extra <= 0 or all(digit == 0 for digit in parts.digits[-extra:])


def _day(value: object, reason: str) -> date:
    try:
        result = date.fromisoformat(value) if isinstance(value, str) else value
        _require(isinstance(result, date) and not isinstance(result, datetime), reason)
        return result
    except ValueError as exc:
        raise HistoricalEvidenceError(reason) from exc


def _instant(value: object, reason: str) -> datetime:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        _require(isinstance(result, datetime) and result.tzinfo is not None and result.utcoffset() is not None, reason)
        return result
    except ValueError as exc:
        raise HistoricalEvidenceError(reason) from exc


def _json(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise HistoricalEvidenceError("invalid_json_evidence") from exc
    return value


def _project(row: dict, names: list[str]) -> dict:
    for name in names:
        _require(name in row, "missing_projection_fact:" + name)
    return {name: row[name] for name in names}


def _project_rows(rows: list[dict], names: list[str], key: str) -> list[dict]:
    return [_project(row, names) for row in sorted(rows, key=lambda item: _id(item[key]))]


def _canonical(value: object) -> object:
    """Explicit typed NULLs, exact decimals, and recursively framed collections."""
    if value is None:
        return ABSENT
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return encode_fields([(str(k), _canonical(v)) for k, v in sorted(value.items())])
    if isinstance(value, (list, tuple)):
        return encode_ordered(_canonical(v) for v in value)
    return value


def _fingerprint(domain: str, value: object) -> str:
    return digest_of(domain, encode_fields([("facts", _canonical(value))]))


def _database(connection: sa.Connection) -> str:
    if connection.dialect.name == "postgresql":
        return connection.execute(sa.text("SELECT current_database()")).scalar_one()
    if connection.dialect.name == "sqlite":
        rows = connection.exec_driver_sql("PRAGMA database_list").all()
        return Path(next(row[2] for row in rows if row[1] == "main")).name
    raise HistoricalEvidenceError("unsupported_database_dialect")


def _readonly(connection: sa.Connection) -> bool:
    if connection.dialect.name == "postgresql":
        return connection.execute(sa.text("SELECT current_setting('transaction_read_only')")).scalar_one() == "on"
    if connection.dialect.name == "sqlite":
        return connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
    return False


def _read(
    connection: sa.Connection,
    name: str,
    *,
    schema: str | None = None,
    key: str | None = None,
    value: object = None,
    fields: list[str] | None = None,
) -> list[dict]:
    if schema is None and connection.dialect.name == "postgresql":
        _require(name in SOURCE_SCHEMAS, "unknown_source_table_contract")
        schema = SOURCE_SCHEMAS[name]
    table = _reflect(connection, name, schema)
    columns = []
    for field in fields if fields is not None else READ_FIELDS[name]:
        _require(field in table.c, f"missing_column:{name}.{field}")
        col = table.c[field]
        if col.name in NUMBERS or isinstance(col.type, sa.Numeric):
            _require(not isinstance(col.type, sa.Float), "unsupported_float_numeric")
            columns.append(sa.cast(col, sa.Text).label(col.name))
        else:
            columns.append(col)
    statement = sa.select(*columns)
    if key is not None:
        _require(key in table.c, f"missing_column:{name}.{key}")
        try:
            ident = UUID(str(value))
        except (ValueError, TypeError):
            statement = statement.where(table.c[key] == value)
        else:
            statement = statement.where(sa.func.replace(sa.cast(table.c[key], sa.Text), "-", "") == ident.hex)
    result = []
    for raw in connection.execute(statement).mappings():
        row = dict(raw)
        for column, item in row.items():
            if item is not None and column in NUMBERS:
                row[column] = _amount(item, f"invalid_numeric:{name}.{column}")
        result.append(row)
    return result


def _one(rows: list[dict], reason: str) -> dict:
    _require(len(rows) == 1, reason)
    return rows[0]


def _match(source: dict, target: dict, mapping: dict[str, str], prefix: str) -> None:
    for dest, origin in mapping.items():
        _require(origin in source and dest in target, f"{prefix}_{dest}_missing")
        left, right = source[origin], target[dest]
        if origin.endswith("_at") and left is not None:
            left, right = _instant(left, prefix + "_" + dest), _instant(right, prefix + "_" + dest)
        _require(_equal(left, right), f"{prefix}_{dest}")


def _pair(source: list[dict], target: list[dict], source_id: str, prefix: str):
    _require(bool(source), prefix + "_missing")
    _require(len(source) == len(target), prefix + "_count")
    sm = {_id(row[source_id]): row for row in source}
    tm = {_id(row["id"]): row for row in target}
    _require(len(sm) == len(source) and len(tm) == len(target) and sm.keys() == tm.keys(), prefix + "_ids")
    return [(sm[key], tm[key]) for key in sorted(sm)]


def _pairs(source: list[dict], target: list[dict], source_id: str, prefix: str, excluded: bool):
    """``_pair``, or (B-13) every source row against None for a cutoff-excluded journal that must be absent in v2."""
    if excluded:
        _require(bool(source), prefix + "_missing")
        _require(not target, prefix + "_present_after_cutoff")
        return [(row, None) for row in source]
    return _pair(source, target, source_id, prefix)


def _tenant(rows: list[dict], tenant: UUID, prefix: str, column: str = "tenant_id") -> None:
    _require(all(column in row and _equal(row[column], tenant) for row in rows), prefix + "_tenant")


@dataclass(frozen=True)
class HistoricalPayablesVerifier:
    source: sa.Connection
    manifest: SnapshotManifest
    source_owner: str = "legacy-erp"
    # (waivable check name, transform rule id) pairs from the importer transform manifest (#68); a failure the
    # transform fixed needs no waiver. Empty until the transform manifest is wired in.
    transforms: tuple[tuple[str, str], ...] = ()
    # The importer's transform manifest (B-3): per-invoice rules and landed values. A destination that differs from the
    # source is accepted only where the manifest names the same values; kept apart from Finance waivers.
    transform_manifest: TransformManifest | None = None
    # The declared fallback expense account (``import.fallback_expense_account``), bound in the plan: the verifier
    # re-derives every transform with it and requires manifest == re-derived == destination.
    fallback_expense_account: str | None = None

    @property
    def history_cutoff(self) -> date | None:
        """The importer's receipt cutoff (B-13), from the hash-bound transform manifest binding; None when absent."""
        return self.transform_manifest.history_cutoff if self.transform_manifest is not None else None

    def _opening_posted(self, dest: sa.Connection, tenant: UUID) -> bool:
        """The B-9 opening journal (owner ``erp.opening``) is POSTED in v2 and dated exactly the manifest cutoff."""
        cutoff = self.history_cutoff
        if cutoff is None:
            return False
        for row in _read(dest, "journal_entries", schema="mod_accounting", key="source_owner", value="erp.opening"):
            if (
                _equal(row["tenant_id"], tenant)
                and row["status"] == "POSTED"
                and _day(row["posting_date"], "opening_posting_date") == cutoff
            ):
                return True
        return False

    def invoice_transform(self, invoice_id: UUID) -> InvoiceTransform | None:
        """The manifest's entry, UNVERIFIED (used only to open the gate); ``rederive_transform`` verifies it."""
        return self.transform_manifest.for_invoice(invoice_id) if self.transform_manifest is not None else None

    def gate_for(self, review: ReviewFacts | None, invoice_id: UUID) -> WaiverGate:
        found = self.invoice_transform(invoice_id)
        return WaiverGate(review, {**dict(self.transforms), **(found.waivable_checks() if found else {})})

    @staticmethod
    def command(evidence: HistoricalUnpaidInvoiceEvidence) -> HistoricalUnpaidInvoiceCommand:
        return HistoricalUnpaidInvoiceCommand(
            source_owner=evidence.source_owner,
            source_invoice_id=evidence.source_invoice_id,
            invoice_id=evidence.invoice_id,
            source_snapshot_sha256=evidence.source_snapshot_sha256,
            evidence_fingerprint=evidence.fingerprint(),
        )

    def verify(
        self, *, scope, command: HistoricalUnpaidInvoiceCommand, destination: Session
    ) -> HistoricalUnpaidInvoiceEvidence:
        command.validate()
        _require(command.source_owner == self.source_owner, "command_source_owner")
        _require(command.source_snapshot_sha256 == self.manifest.snapshot_sha256, "command_snapshot")
        _require(command.source_invoice_id == str(command.invoice_id), "command_source_invoice")
        evidence = self.evidence(scope=scope, invoice_id=command.invoice_id, destination=destination)
        _require(command.evidence_fingerprint == evidence.fingerprint(), "command_fingerprint")
        return evidence

    def evidence(self, *, scope, invoice_id: UUID, destination: Session) -> HistoricalUnpaidInvoiceEvidence:
        """Preview immutable proof; later verify rebuilds it inside the owner's lock."""
        _require(isinstance(self.source, sa.Connection), "source_connection_required")
        _require(isinstance(destination, Session), "destination_session_required")
        _require(not destination.new and not destination.dirty and not destination.deleted, "destination_dirty_session")
        _require(isinstance(invoice_id, UUID), "invoice_id_required")
        tenant = getattr(scope, "tenant_id", None)
        _require(isinstance(tenant, UUID), "scope_tenant_required")
        manifest = self.manifest
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", manifest.snapshot_sha256)), "manifest_snapshot")
        _require(bool(manifest.candidate_revision) and bool(self.source_owner), "manifest_candidate")
        _require(manifest.source_database != manifest.destination_database, "manifest_database_separation")
        _require(str(tenant) in {_id(value) for value in manifest.tenant_extent}, "tenant_extent")
        _require(_readonly(self.source), "source_not_readonly")
        _require(_database(self.source) == manifest.source_database, "source_database")
        with destination.no_autoflush:
            connection = destination.connection()
            _require(_database(connection) == manifest.destination_database, "destination_database")
            gate = self.gate_for(
                load_review(connection, tenant, invoice_id, manifest.snapshot_sha256, allowed=ADMISSION), invoice_id
            )
            try:
                result = self._evidence(tenant, invoice_id, connection, gate)
            except KeyError as exc:
                raise HistoricalEvidenceError(f"missing_required_fact:{exc.args[0]}") from exc
            gate.finish()
            return result

    def review(self, *, scope, invoice_id: UUID, destination: Session) -> ReviewFacts | None:
        """The Finance decision for this invoice (None if none), read in the destination under the same manifest."""
        return load_review(
            destination.connection(), scope.tenant_id, invoice_id, self.manifest.snapshot_sha256, allowed=ADMISSION
        )

    def _evidence(
        self, tenant: UUID, invoice_id: UUID, dest: sa.Connection, gate: WaiverGate
    ) -> HistoricalUnpaidInvoiceEvidence:
        header, common = self._parts(tenant, invoice_id, dest, gate, settled=False)
        return HistoricalUnpaidInvoiceEvidence(
            # A waived status failure is proved by the POSTED legacy journal (``_accounting``); the owner's
            # contract takes POSTED, and the real header status stays in the facts fingerprint.
            source_status="POSTED" if gate.used & STATUS_WAIVERS else header["status"],
            paid_amount=header["amount_paid"],
            allocation_count=0,
            withholding_tax_amount=header["withholding_tax_amount"],
            **common,
        )

    def _parts(
        self, tenant: UUID, invoice_id: UUID, dest: sa.Connection, gate: WaiverGate, *, settled: bool
    ) -> tuple[dict, dict]:
        """Shared invoice, accounting and destination proof. ``settled`` only relaxes the unpaid shape
        (status PARTIALLY_PAID, amount_paid > 0, allocations present); every other fact is identical."""
        src = self.source
        header = _one(
            _read(src, "supplier_invoice", key="invoice_id", value=invoice_id), "source_invoice_missing_or_duplicate"
        )
        _tenant([header], tenant, "source_invoice", "organization_id")
        _require(header["invoice_type"] == "STANDARD", "source_type")
        if settled:
            gate.require(
                header["status"] in ("POSTED", "PARTIALLY_PAID") and header["posting_status"] == "POSTED",
                "source_status",
                _status_waiver(header, settled=True),
            )
            _require(header["amount_paid"] > 0, "source_not_paid")
        else:
            gate.require(
                header["status"] == "POSTED" and header["posting_status"] == "POSTED",
                "source_status",
                _status_waiver(header, settled=False),
            )
            _require(header["amount_paid"] == 0, "source_paid")
        _require(
            header["withholding_tax_amount"] == 0 and header["withholding_tax_code_id"] is None, "source_withholding"
        )
        _require(header["is_prepayment"] is False and header["prepayment_applied"] == 0, "source_prepayment")
        _require(header["stamp_duty_amount"] == 0, "unsupported_stamp_duty")
        if not settled:
            if "source_allocations" in gate.waived and _read(
                src, "payment_allocation", key="invoice_id", value=invoice_id
            ):
                _allocations_without_control_effect(src, invoice_id, header["ap_control_account_id"])
            gate.require(
                not _read(src, "payment_allocation", key="invoice_id", value=invoice_id),
                "source_allocations",
                "source_allocations",
            )
        for field in ("subtotal", "tax_amount", "total_amount"):
            amount = header[field]
            # Exact remainder, not quantization: six-place source storage must never round.
            _require(isinstance(amount, Decimal) and amount >= 0, "source_amount_scale")
            gate.require(_at_scale(amount, 2), "source_amount_scale", "source_amount_scale")
        _require(
            header["total_amount"] > 0 and _sum([header["subtotal"], header["tax_amount"]]) == header["total_amount"],
            "source_total",
        )
        due = _day(header["due_date"], "source_due_date")
        # An unposted header linked to a legacy journal has no posted_at of its own: with the waiver the
        # (verified POSTED) legacy journal's posting instant is the original posting instant.
        if header["posted_at"] is None and gate.waived_used("legacy_journal_link_unposted"):
            linked = _one(
                _read(src, "journal_entry", key="journal_entry_id", value=header["journal_entry_id"]),
                "source_journal_missing",
            )
            posted = _instant(linked["posted_at"], "source_posted_at")
        else:
            posted = _instant(header["posted_at"], "source_posted_at")
        organization = _one(_read(src, "organization", key="organization_id", value=tenant), "source_organization")
        _require(
            header["exchange_rate"] == 1
            and organization["functional_currency_code"] == header["currency_code"]
            and header["functional_currency_amount"] == header["total_amount"],
            "unsupported_currency",
        )
        supplier = _one(_read(src, "supplier", key="supplier_id", value=header["supplier_id"]), "source_supplier")
        _tenant([supplier], tenant, "source_supplier", "organization_id")
        name = supplier["legal_name"] if supplier["legal_name"] not in (None, "") else supplier["trading_name"]
        _require(isinstance(name, str) and bool(name), "source_supplier_name")
        target = _one(
            _read(dest, "supplier_invoices", schema="mod_payables", key="id", value=invoice_id),
            "destination_invoice_missing_or_duplicate",
        )
        _tenant([target], tenant, "destination_invoice")
        mapping = {
            "id": "invoice_id",
            "number": "invoice_number",
            "supplier_ref": "supplier_id",
            "supplier_document_number": "supplier_invoice_number",
            "liability_account_ref": "ap_control_account_id",
            "submitted_by": "submitted_by_user_id",
            "approved_by": "approved_by_user_id",
        }
        mapping.update(
            {
                key: key
                for key in (
                    "invoice_date",
                    "received_date",
                    "currency_code",
                    "exchange_rate",
                    "subtotal",
                    "tax_amount",
                    "total_amount",
                    "submitted_at",
                    "approved_at",
                    "created_at",
                )
            }
        )
        found = rederive_transform(self, tenant, invoice_id, header)
        effective = found.header_values(header) if found else header
        _match(effective, target, mapping, "header")
        _require(target["supplier_name_snapshot"] == name, "header_supplier_name_snapshot")
        # The legacy approval request is optional evidence, never a fabricated stamp.
        _require(_equal(target["approval_reference"], header["approval_request_id"]), "header_approval_reference")
        _require(
            target["procurement_ref"] is None and target["receipt_evidence_fingerprint"] is None,
            "unsupported_procurement_evidence",
        )
        schedule = _json(target["payment_schedule"])
        _require(
            isinstance(schedule, list)
            and len(schedule) == 1
            and isinstance(schedule[0], dict)
            and set(schedule[0]) == {"due_date", "amount"},
            "header_payment_schedule",
        )
        _require(
            _day(schedule[0]["due_date"], "header_due_date") == due
            and _amount(schedule[0]["amount"], "header_schedule_amount") == effective["total_amount"],
            "header_payment_schedule",
        )
        lines = _read(src, "supplier_invoice_line", key="invoice_id", value=invoice_id)
        targets = _read(dest, "supplier_invoice_lines", schema="mod_payables", key="invoice_id", value=invoice_id)
        _tenant(targets, tenant, "invoice_line")
        outcome, tax_evidence = invoice_tax(
            src,
            tenant,
            header["journal_entry_id"],
            lines,
            invoice_id,
            header["ap_control_account_id"],
            header["subtotal"],
            header["total_amount"],
        )
        line_mapping = {
            k: k
            for k in (
                "invoice_id",
                "line_number",
                "description",
                "quantity",
                "unit_price",
                "line_amount",
                "tax_amount",
                "created_at",
            )
        }
        line_mapping["posting_account_ref"] = "expense_account_id"
        for original, imported in _pair(lines, targets, "line_id", "invoice_line"):
            _require(
                all(
                    isinstance(original[field], Decimal) and original[field] >= 0
                    for field in ("quantity", "unit_price", "line_amount", "tax_amount")
                )
                and original["quantity"] > 0,
                "source_invoice_line_amounts",
            )
            _match(found.line_values(original) if found else original, imported, line_mapping, "invoice_line")
            _require(
                original["po_line_id"] is None and original["goods_receipt_line_id"] is None,
                "unsupported_procurement_link",
            )
            require_line_tax_account(outcome, original["line_id"], imported, found)
            dimensions = sorted(
                [[code, _id(original[col])] for code, col in DIMENSIONS[1:] if original[col] is not None]
            )
            actual = _json(imported["dimension_refs"])
            _require(
                isinstance(actual, list) and all(isinstance(pair, list) and len(pair) == 2 for pair in actual),
                "invoice_line_dimensions",
            )
            _require(sorted([[pair[0], _id(pair[1])] for pair in actual]) == dimensions, "invoice_line_dimensions")
        # Legacy-posted invoices must show a GL treatment the rule recognises (A1 split / A2 folded).
        gate.require(
            outcome.klass != "A3",
            "legacy_gl_tax_treatment_unrecognised",
            ("deferral_amount_mismatch", "legacy_gl_tax_treatment_unrecognised")
            if outcome.reason == "deferral_amount_mismatch"
            else "legacy_gl_tax_treatment_unrecognised",
            flag=outcome.reason,
        )
        # Inclusive line prices are evidence; never recompute quantity * unit_price.
        gate.require(
            _sum([row["line_amount"] for row in lines]) == header["subtotal"]
            and _sum([row["tax_amount"] for row in lines]) == header["tax_amount"],
            "invoice_line_totals",
            "line_totals",
        )
        accounting = self._accounting(tenant, header, dest, gate)
        self._deferral_journals(tenant, header, outcome, dest)
        # Only immutable imported fields enter the replay evidence. Current owner
        # invoice status, obligations, events and settlements deliberately do not.
        facts = _fingerprint(
            "erp.payables.historical-facts.v1",
            dict(
                manifest=self.manifest.as_dict(),
                source_header=_project(header, SOURCE_HEADER),
                source_supplier=_project(supplier, SOURCE_SUPPLIER),
                source_organization=_project(organization, SOURCE_ORGANIZATION),
                source_lines=_project_rows(lines, SOURCE_INVOICE_LINE, "line_id"),
                destination_header=_project(target, DESTINATION_HEADER),
                destination_lines=_project_rows(targets, DESTINATION_INVOICE_LINE, "id"),
                tax_accounts=dict(
                    invoice_class=outcome.klass,
                    a1_expense_elsewhere=outcome.expense_elsewhere,
                    **_deferral_evidence(outcome),
                    lines=[
                        dict(line_id=line_id, klass=item.klass, ref=item.ref)
                        for line_id, item in sorted(outcome.lines.items())
                    ],
                    codes=tax_evidence,
                ),
                **gate.facts(),
                **({"transform_manifest": transform_facts(found, self.transform_manifest)} if found else {}),
            ),
        )
        common = dict(
            tenant_id=tenant,
            source_owner=self.source_owner,
            source_invoice_id=str(invoice_id),
            source_snapshot_sha256=self.manifest.snapshot_sha256,
            invoice_id=invoice_id,
            invoice_type=header["invoice_type"],
            prepayment_amount=header["prepayment_applied"],
            number=target["number"],
            supplier_ref=target["supplier_ref"],
            supplier_name_snapshot=name,
            supplier_document_number=target["supplier_document_number"],
            invoice_date=_day(header["invoice_date"], "source_invoice_date"),
            received_date=_day(header["received_date"], "source_received_date"),
            due_date=due,
            currency_code=header["currency_code"],
            exchange_rate=header["exchange_rate"],
            # What the owner admission receives is the LANDED invoice (the destination carries it); the source values
            # stay as provenance in ``facts_fingerprint`` (source_header).
            subtotal=effective["subtotal"],
            tax_amount=effective["tax_amount"],
            total_amount=effective["total_amount"],
            liability_account_ref=target["liability_account_ref"],
            procurement_ref=None,
            receipt_evidence_fingerprint=None,
            submitted_by=target["submitted_by"],
            approved_by=target["approved_by"],
            approval_reference=target["approval_reference"],
            submitted_at=_instant(header["submitted_at"], "source_submitted_at")
            if header["submitted_at"] is not None
            else None,
            approved_at=_instant(header["approved_at"], "source_approved_at")
            if header["approved_at"] is not None
            else None,
            original_posted_at=posted,
            source_journal_ref=_id(header["journal_entry_id"]),
            destination_journal_ref=_id(header["journal_entry_id"]),
            accounting_evidence_fingerprint=accounting,
            facts_fingerprint=facts,
        )
        return header, common

    def _accounting(self, tenant: UUID, header: dict, dest: sa.Connection, gate: WaiverGate) -> str:
        journal_id = header["journal_entry_id"]
        _require(journal_id is not None, "source_journal_missing")
        _journal_not_shared(self.source, dest, journal_id, header["invoice_id"])
        if gate.review is not None and gate.review.decision == "admit_with_waivers":
            _require(
                gate.review.legacy_journal_id is not None and _equal(gate.review.legacy_journal_id, journal_id),
                "review_journal_mismatch",
            )
        journal = _one(
            _read(self.source, "journal_entry", key="journal_entry_id", value=journal_id), "source_journal_missing"
        )
        _tenant([journal], tenant, "source_journal", "organization_id")
        gate.require(
            journal["source_module"] == "AP" and journal["source_document_type"] == "SUPPLIER_INVOICE",
            "source_journal_owner",
            "journal_owner",
        )
        gate.require(
            _equal(journal["source_document_id"], header["invoice_id"]), "source_journal_invoice", "journal_owner"
        )
        _require(journal["status"] == "POSTED", "source_journal_status")
        _require(
            journal["is_reversal"] is False
            and journal["journal_type"] != "REVERSAL"
            and journal["reversed_journal_id"] is None
            and journal["reversal_journal_id"] is None,
            "source_journal_reversal",
        )
        _require(
            journal["currency_code"] == header["currency_code"] and journal["exchange_rate"] == 1,
            "unsupported_currency",
        )
        ledger = _read(self.source, "posted_ledger_line", key="journal_entry_id", value=journal_id)
        proof = journal_proof(
            journal, ledger, self.history_cutoff, _require, partial(self._opening_posted, dest, tenant)
        )
        excluded = proof is not None
        destination_rows = _read(dest, "journal_entries", schema="mod_accounting", key="id", value=journal_id)
        if excluded:
            # B-13: the journal is in the opening TB, not in v2. Proven on the snapshot above, asserted absent here.
            _require(not destination_rows, "cutoff_journal_present_in_destination")
            target = None
        else:
            target = _one(destination_rows, "destination_journal_missing")
            _tenant([target], tenant, "destination_journal")
        if target is not None:
            mapping = {
                key: key
                for key in (
                    "status",
                    "currency_code",
                    "exchange_rate",
                    "entry_date",
                    "posting_date",
                    "fiscal_period_id",
                    "description",
                    "reference",
                    "total_debit",
                    "total_credit",
                    "total_debit_functional",
                    "total_credit_functional",
                    "posted_at",
                    "created_at",
                )
            }
            mapping.update(
                number="journal_number",
                kind="journal_type",
                posted_by="posted_by_user_id",
                reverses_journal_id="reversed_journal_id",
                reversal_journal_id="reversal_journal_id",
                source_document_kind="source_document_type",
            )
            _match(journal, target, mapping, "journal")
            # waves36 deliberately uses the legacy journal row as destination document
            # identity, retaining the AP invoice link in source provenance.
            _require(
                target["source_document_id"] == f"{SOURCE_JOURNAL_REFERENCE}{journal['journal_entry_id']}",
                "journal_source_document_id",
            )
            expected_posting_stamp = (
                f"{SOURCE_JOURNAL_REFERENCE}{target['id']}@"
                f"{_day(journal['posting_date'], 'source_journal_posting_date').isoformat()}"
            )
            _require(target["approval_reference"] == expected_posting_stamp, "journal_posting_stamp")
            self._provenance(target, "journal")
        originals = _read(self.source, "journal_entry_line", key="journal_entry_id", value=journal_id)
        imported = _read(dest, "journal_lines", schema="mod_accounting", key="journal_id", value=journal_id)
        _tenant(imported, tenant, "journal_line")
        if excluded:
            _require(not imported, "cutoff_journal_line_present_in_destination")
        line_map = {key: key for key in ("line_number", "account_id", "description", "created_at")}
        line_map.update(
            journal_id="journal_entry_id",
            debit="debit_amount",
            credit="credit_amount",
            debit_functional="debit_amount_functional",
            credit_functional="credit_amount_functional",
        )
        dimensions = []
        for original, landed in _pairs(originals, imported, "line_id", "journal_line", excluded):
            _require(
                all(
                    isinstance(original[field], Decimal) and original[field] >= 0
                    for field in (
                        "debit_amount",
                        "credit_amount",
                        "debit_amount_functional",
                        "credit_amount_functional",
                    )
                )
                and (
                    (original["debit_amount"] > 0 and original["credit_amount"] == 0)
                    or (original["credit_amount"] > 0 and original["debit_amount"] == 0)
                ),
                "source_journal_line_amounts",
            )
            if landed is not None:
                _match(original, landed, line_map, "journal_line")
            _require(
                original["currency_code"] in (None, header["currency_code"])
                and original["exchange_rate"] in (None, Decimal(1)),
                "unsupported_currency",
            )
            _require(
                original["debit_amount"] == original["debit_amount_functional"]
                and original["credit_amount"] == original["credit_amount_functional"],
                "unsupported_currency",
            )
            if landed is not None:
                dimensions.append(self._dimensions(tenant, original, landed["id"], dest, ledger=False))
        for side in ("debit", "credit"):
            total = _sum([row[side + "_amount"] for row in originals])
            _require(
                total == journal["total_" + side] == journal["total_" + side + "_functional"], "source_journal_totals"
            )
        _require(journal["total_debit"] == journal["total_credit"], "source_journal_unbalanced")
        control = [row for row in originals if _equal(row["account_id"], header["ap_control_account_id"])]
        _require(
            bool(control)
            and all(row["debit_amount"] == 0 and row["credit_amount"] > 0 for row in control)
            and _sum([row["credit_amount"] for row in control]) == header["total_amount"],
            "source_ap_control_credit",
        )
        posted = _read(dest, "posted_ledger_lines", schema="mod_accounting", key="journal_id", value=journal_id)
        _tenant(ledger, tenant, "source_ledger", "organization_id")
        _tenant(posted, tenant, "ledger")
        if excluded:
            _require(not posted, "cutoff_journal_ledger_present_in_destination")
        _require(len(ledger) == len(originals), "source_ledger_line_count")
        by_line = {_id(row["line_id"]): row for row in originals}
        _require(len({_id(row["journal_line_id"]) for row in ledger}) == len(ledger), "source_ledger_line_duplicate")
        ledger_map = {
            key: key
            for key in (
                "journal_line_id",
                "account_id",
                "account_code",
                "fiscal_period_id",
                "entry_date",
                "posting_date",
                "description",
                "posted_at",
            )
        }
        ledger_map.update(
            journal_id="journal_entry_id",
            debit="debit_amount",
            credit="credit_amount",
            source_document_kind="source_document_type",
            source_document_id="source_document_id",
            posted_by="posted_by_user_id",
        )
        for original, landed in _pairs(ledger, posted, "ledger_line_id", "ledger", excluded):
            if landed is not None:
                _match(original, landed, ledger_map, "ledger")
                self._provenance(landed, "ledger")
                _require(landed["journal_number"] == journal["journal_number"], "ledger_journal_number")
            gate.require(
                original["source_module"] == "AP"
                and original["source_document_type"] == "SUPPLIER_INVOICE"
                and _equal(original["source_document_id"], header["invoice_id"]),
                "source_ledger_invoice",
                "journal_owner",
            )
            _require(
                original["original_currency_code"] in (None, header["currency_code"])
                and original["exchange_rate"] in (None, Decimal(1))
                and (
                    landed is None
                    or (landed["currency_code"] == header["currency_code"] and landed["exchange_rate"] == 1)
                ),
                "unsupported_currency",
            )
            line = by_line.get(_id(original["journal_line_id"]))
            _require(line is not None and _equal(original["account_id"], line["account_id"]), "source_ledger_line")
            for side in ("debit", "credit"):
                value = original["original_" + side + "_amount"]
                value = original[side + "_amount"] if value is None else value
                _require(
                    value == original[side + "_amount"] == line[side + "_amount"]
                    and (landed is None or landed["original_" + side] == value),
                    "ledger_original_" + side,
                )
            if landed is not None:
                dimensions.append(self._dimensions(tenant, original, landed["id"], dest, ledger=True))
        return _fingerprint(
            "erp.payables.historical-accounting.v1",
            dict(
                source_journal=_project(journal, SOURCE_JOURNAL),
                destination_journal=None if target is None else _project(target, DESTINATION_JOURNAL),
                source_lines=_project_rows(originals, SOURCE_JOURNAL_LINE, "line_id"),
                destination_lines=_project_rows(imported, DESTINATION_JOURNAL_LINE, "id"),
                source_ledger=_project_rows(ledger, SOURCE_LEDGER, "ledger_line_id"),
                destination_ledger=_project_rows(posted, DESTINATION_LEDGER, "id"),
                dimensions=dimensions,
                **({"history_cutoff_proof": proof} if proof is not None else {}),
            ),
        )

    def _deferral_journals(
        self, tenant: UUID, header: dict, outcome: tax_accounts.InvoiceTax, dest: sa.Connection
    ) -> None:
        """The legacy VAT-deferral journals the classification relied on must be in v2, POSTED and equal.

        Their ids are in the fingerprint (``_deferral_evidence``); like ``_accounting`` for the invoice journal, the
        source journal must be the invoice's POSTED deferral document and the destination must hold it POSTED with
        the same totals and the same posted ledger lines (account, debit, credit).
        """

        for journal_id in outcome.deferral_journal_ids:
            journal = _one(
                _read(self.source, "journal_entry", key="journal_entry_id", value=journal_id),
                "deferral_journal_missing",
            )
            _tenant([journal], tenant, "deferral_journal", "organization_id")
            _require(
                journal["source_module"] == "AP"
                and journal["source_document_type"] == tax_accounts.DEFERRAL_DOCUMENT_TYPE
                and _equal(journal["source_document_id"], header["invoice_id"])
                and journal["status"] == "POSTED",
                "deferral_journal_source",
            )
            ledger = _read(self.source, "posted_ledger_line", key="journal_entry_id", value=journal_id)
            posted = _read(dest, "posted_ledger_lines", schema="mod_accounting", key="journal_id", value=journal_id)
            destination_rows = _read(dest, "journal_entries", schema="mod_accounting", key="id", value=journal_id)
            if (
                journal_proof(
                    journal, ledger, self.history_cutoff, _require, partial(self._opening_posted, dest, tenant)
                )
                is not None
            ):
                # B-13: the deferral journal is in the opening TB (proven on the snapshot), absent from v2.
                _require(not destination_rows, "cutoff_deferral_journal_present_in_destination")
                _require(not posted, "cutoff_deferral_ledger_present_in_destination")
                _require(len(ledger) > 0, "deferral_journal_ledger")
                continue
            target = _one(destination_rows, "destination_deferral_journal_missing")
            _tenant([target], tenant, "destination_deferral_journal")
            _require(target["status"] == "POSTED", "destination_deferral_journal_status")
            _match(
                journal,
                target,
                {key: key for key in ("currency_code", "exchange_rate", "entry_date", "posting_date")}
                | dict(total_debit="total_debit", total_credit="total_credit"),
                "deferral_journal",
            )
            _require(
                target["source_document_id"] == f"{SOURCE_JOURNAL_REFERENCE}{journal['journal_entry_id']}",
                "deferral_journal_source_document_id",
            )
            self._provenance(target, "deferral_journal")
            _tenant(posted, tenant, "deferral_ledger")
            _require(
                sorted((_id(r["account_id"]), r["debit_amount"], r["credit_amount"]) for r in ledger)
                == sorted((_id(r["account_id"]), r["debit"], r["credit"]) for r in posted)
                and len(ledger) > 0,
                "deferral_journal_ledger",
            )

    def _provenance(self, row: dict, prefix: str) -> None:
        _require(row["source_owner"] == self.source_owner, prefix + "_source_owner")
        _require(row["source_version"] == self.manifest.candidate_revision, prefix + "_source_version")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", row["source_fingerprint"])), prefix + "_source_fingerprint")
        if "request_fingerprint" in row:
            _require(bool(re.fullmatch(r"[0-9a-f]{64}", row["request_fingerprint"])), prefix + "_request_fingerprint")

    def _dimensions(
        self, tenant: UUID, source: dict, line_id: object, dest: sa.Connection, *, ledger: bool
    ) -> list[dict]:
        table = "posted_ledger_dimensions" if ledger else "journal_line_dimensions"
        key = "ledger_line_id" if ledger else "journal_line_id"
        rows = _read(dest, table, schema="mod_accounting", key=key, value=line_id)
        _tenant(rows, tenant, "accounting_dimension")
        expected = {code: _id(source[col]) for code, col in DIMENSIONS if source[col] is not None}
        _require(len(expected) == len(rows), "accounting_dimensions_count")
        actual = {}
        frozen = []
        for row in sorted(rows, key=lambda item: (_id(item["dimension_id"]), _id(item["dimension_value_id"]))):
            definition = _one(
                _read(dest, "accounting_dimensions", schema="mod_accounting", key="id", value=row["dimension_id"]),
                "accounting_dimension_missing",
            )
            value = _one(
                _read(
                    dest,
                    "accounting_dimension_values",
                    schema="mod_accounting",
                    key="id",
                    value=row["dimension_value_id"],
                ),
                "accounting_dimension_value_missing",
            )
            _tenant([definition, value], tenant, "accounting_dimension")
            code = definition["code"]
            _require(
                code not in actual and _equal(value["dimension_id"], definition["id"]),
                "accounting_dimension_duplicate_or_parent",
            )
            actual[code] = _id(value["id"])
            if ledger:
                _require(
                    row["dimension_code"] == code and row["value_code"] == value["code"], "ledger_dimension_snapshot"
                )
            names = (
                "id tenant_id ledger_line_id dimension_id dimension_code dimension_value_id value_code".split()
                if ledger
                else "id tenant_id journal_line_id dimension_id dimension_value_id".split()
            )
            frozen.append(
                dict(
                    row=_project(row, names),
                    definition=_project(definition, ["id", "tenant_id", "code"]),
                    value=_project(value, ["id", "tenant_id", "dimension_id", "code"]),
                )
            )
        _require(actual == expected, "accounting_dimensions")
        return frozen


__all__ = ["HistoricalEvidenceError", "HistoricalPayablesVerifier"]
