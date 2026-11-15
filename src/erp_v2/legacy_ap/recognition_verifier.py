"""Trusted, SELECT-only verifier for payables ``recognize_approved_invoice`` (A5-3).

The ERP side of the owner's verifier contract. It proves, from the restored legacy
snapshot read-only, that a legacy supplier invoice was APPROVED and NEVER posted:

* the legacy header status is exactly ``APPROVED`` and its posting status is not POSTED;
* ``journal_entry_id`` and ``posted_at`` are NULL;
* no legacy journal entry names the invoice as its source document, and there are no
  journal lines or posted ledger lines for it (``source_gl_line_count == 0``);
* nothing was paid (``amount_paid`` 0, no payment allocation, no withholding, no prepayment);
* the destination invoice (number, supplier, currency, amounts, per-line amounts) equals
  the legacy row exactly.

Recognizing and then posting a legacy-POSTED invoice would double count expense, VAT and
the AP control account, so any doubt refuses with a named reason. The verifier never
constructs connections, writes, or commits. A manifest digest cannot by itself prove the
bytes a restored database came from; the operator's restore receipt attests that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_payables import ApprovedInvoiceRecognitionEvidence, RecognizeApprovedInvoice
from sqlalchemy.orm import Session

from erp_v2.importer.payables_history import (
    DESTINATION_HEADER,
    DESTINATION_INVOICE_LINE,
    SOURCE_HEADER,
    SOURCE_INVOICE_LINE,
    HistoricalEvidenceError,
    _amount,
    _at_scale,
    _database,
    _fingerprint,
    _id,
    _match,
    _one,
    _pair,
    _project,
    _project_rows,
    _read,
    _readonly,
    _require,
    _tenant,
)
from erp_v2.importer.payables_reconciliation import _sum
from erp_v2.importer.snapshot import SnapshotManifest

SOURCE_OWNER = "legacy-erp"
FACTS_DOMAIN = "erp.payables.recognition-facts.v1"
NOT_POSTED_STATUS = "APPROVED"


@dataclass(frozen=True)
class ApprovedInvoiceVerifier:
    source: sa.Connection
    manifest: SnapshotManifest
    evidence_reference: str
    source_owner: str = SOURCE_OWNER

    def command(
        self, evidence: ApprovedInvoiceRecognitionEvidence, *, recognized_by: str, approval_reference: str
    ) -> RecognizeApprovedInvoice:
        return RecognizeApprovedInvoice(
            invoice_id=evidence.invoice_id,
            evidence_reference=self.evidence_reference,
            recognized_by=recognized_by,
            approval_reference=approval_reference,
            source_owner=evidence.source_owner,
            source_invoice_id=evidence.source_invoice_id,
            source_snapshot_sha256=evidence.source_snapshot_sha256,
            evidence_fingerprint=evidence.fingerprint(),
        )

    def verify(
        self, *, scope, command: RecognizeApprovedInvoice, destination: Session
    ) -> ApprovedInvoiceRecognitionEvidence:
        command.validate()
        _require(command.source_owner == self.source_owner, "command_source_owner")
        _require(command.source_snapshot_sha256 == self.manifest.snapshot_sha256, "command_snapshot")
        _require(command.source_invoice_id == str(command.invoice_id), "command_source_invoice")
        _require(command.evidence_reference == self.evidence_reference, "command_evidence_reference")
        evidence = self.evidence(scope=scope, invoice_id=command.invoice_id, destination=destination)
        _require(command.evidence_fingerprint == evidence.fingerprint(), "command_fingerprint")
        return evidence

    def evidence(self, *, scope, invoice_id: UUID, destination: Session) -> ApprovedInvoiceRecognitionEvidence:
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
            try:
                return self._evidence(tenant, invoice_id, connection)
            except KeyError as exc:
                raise HistoricalEvidenceError(f"missing_required_fact:{exc.args[0]}") from exc

    def _evidence(self, tenant: UUID, invoice_id: UUID, dest: sa.Connection) -> ApprovedInvoiceRecognitionEvidence:
        src = self.source
        header = _one(
            _read(src, "supplier_invoice", key="invoice_id", value=invoice_id), "source_invoice_missing_or_duplicate"
        )
        _tenant([header], tenant, "source_invoice", "organization_id")
        _require(header["invoice_type"] == "STANDARD", "source_type")
        _require(header["status"] == NOT_POSTED_STATUS, "source_status_not_approved")
        _require(header["posting_status"] != "POSTED", "source_posting_status_posted")
        _require(header["journal_entry_id"] is None, "source_journal_present")
        _require(header["posted_at"] is None, "source_posted_at_present")
        _require(header["amount_paid"] == 0, "source_paid")
        _require(
            header["withholding_tax_amount"] == 0 and header["withholding_tax_code_id"] is None, "source_withholding"
        )
        _require(header["is_prepayment"] is False and header["prepayment_applied"] == 0, "source_prepayment")
        _require(header["stamp_duty_amount"] == 0, "unsupported_stamp_duty")
        _require(not _read(src, "payment_allocation", key="invoice_id", value=invoice_id), "source_allocations")
        for field in ("subtotal", "tax_amount", "total_amount"):
            amount = header[field]
            # Exact remainder, not quantization: source storage must never round.
            _require(isinstance(amount, Decimal) and amount >= 0 and _at_scale(amount, 2), "source_amount_scale")
        _require(
            header["total_amount"] > 0 and _sum([header["subtotal"], header["tax_amount"]]) == header["total_amount"],
            "source_total",
        )
        # No legacy GL footprint of any kind: journals, journal lines, posted ledger lines.
        journals = _read(src, "journal_entry", key="source_document_id", value=invoice_id)
        _require(not journals, "source_gl_journal_present")
        ledger = _read(src, "posted_ledger_line", key="source_document_id", value=invoice_id)
        _require(not ledger, "source_gl_ledger_present")
        gl_line_count = len(ledger)
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
                )
            }
        )
        _match(header, target, mapping, "header")
        lines = _read(src, "supplier_invoice_line", key="invoice_id", value=invoice_id)
        targets = _read(dest, "supplier_invoice_lines", schema="mod_payables", key="invoice_id", value=invoice_id)
        _tenant(targets, tenant, "invoice_line")
        line_mapping = {k: k for k in ("invoice_id", "line_number", "description", "line_amount", "tax_amount")}
        line_mapping["posting_account_ref"] = "expense_account_id"
        for original, imported in _pair(lines, targets, "line_id", "invoice_line"):
            _match(original, imported, line_mapping, "invoice_line")
        _require(
            _sum([row["line_amount"] for row in lines]) == header["subtotal"]
            and _sum([row["tax_amount"] for row in lines]) == header["tax_amount"],
            "invoice_line_totals",
        )
        facts = _fingerprint(
            FACTS_DOMAIN,
            dict(
                manifest=self.manifest.as_dict(),
                source_header=_project(header, SOURCE_HEADER),
                source_lines=_project_rows(lines, SOURCE_INVOICE_LINE, "line_id"),
                destination_header=_project(target, DESTINATION_HEADER),
                destination_lines=_project_rows(targets, DESTINATION_INVOICE_LINE, "id"),
                gl=dict(journals=0, ledger_lines=gl_line_count),
            ),
        )
        return ApprovedInvoiceRecognitionEvidence(
            tenant_id=tenant,
            source_owner=self.source_owner,
            source_invoice_id=str(invoice_id),
            source_snapshot_sha256=self.manifest.snapshot_sha256,
            invoice_id=invoice_id,
            source_status=header["status"],
            source_journal_entry_id=None,
            source_posted_at=None,
            source_gl_line_count=gl_line_count,
            number=target["number"],
            supplier_ref=target["supplier_ref"],
            currency_code=target["currency_code"],
            total_amount=_amount(target["total_amount"], "destination_total_amount"),
            facts_fingerprint=facts,
        )
