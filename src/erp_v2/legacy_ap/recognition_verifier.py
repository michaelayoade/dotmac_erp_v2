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
    ReviewFacts,
    WaiverGate,
    _allocations_without_control_effect,
    _amount,
    _at_scale,
    _database,
    _day,
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
    invoice_tax,
    load_review,
    rederive_transform,
    require_line_tax_account,
    transform_facts,
)
from erp_v2.importer.payables_reconciliation import _reflect, _sum
from erp_v2.importer.snapshot import SnapshotManifest
from erp_v2.importer.tax_accounts import DEFERRAL_DOCUMENT_TYPE, canonical
from erp_v2.legacy_ap.transform_manifest import InvoiceTransform, TransformManifest

SOURCE_OWNER = "legacy-erp"
FACTS_DOMAIN = "erp.payables.recognition-facts.v1"
NOT_POSTED_STATUS = "APPROVED"
NOT_POSTED_POSTING_STATUS = "NOT_POSTED"
# B1b honours a Finance waiver and the IAS 8 posting decision (D-M5); any other decision refuses the invoice.
RECOGNITION_DECISIONS = frozenset({"admit_residual", "admit_with_waivers", "ias8_post"})


def _hex_in(column, values) -> sa.ColumnElement:
    return sa.func.replace(sa.cast(column, sa.Text), "-", "").in_([UUID(str(v)).hex for v in values])


def _linked_rows(src: sa.Connection, name: str, schema: str, conditions: dict[str, list]) -> int:
    """Rows of a legacy table matching ANY id in ``conditions`` (column -> ids); fail closed if absent."""
    try:
        table = _reflect(src, name, schema if src.dialect.name == "postgresql" else None)
    except (sa.exc.NoSuchTableError, ValueError) as exc:
        raise HistoricalEvidenceError(f"source_table_missing:{name}") from exc
    clauses = []
    for column, values in conditions.items():
        if column not in table.c:
            raise HistoricalEvidenceError(f"source_column_missing:{name}.{column}")
        if values:
            clauses.append(_hex_in(table.c[column], values))
    if not clauses:
        return 0
    return src.execute(sa.select(sa.func.count()).select_from(table).where(sa.or_(*clauses))).scalar_one()


def _item_line_ids(src: sa.Connection, invoice_id: UUID) -> set[str]:
    """Ids of the invoice lines carrying an inventory item (legacy ``item_id``)."""
    table = _reflect(src, "supplier_invoice_line", "ap" if src.dialect.name == "postgresql" else None)
    for column in ("item_id", "invoice_id", "line_id"):
        if column not in table.c:
            raise HistoricalEvidenceError(f"source_column_missing:supplier_invoice_line.{column}")
    rows = src.execute(
        sa.select(table.c.line_id).where(_hex_in(table.c.invoice_id, [invoice_id]), table.c.item_id.is_not(None))
    )
    return {str(row[0]) for row in rows}


def item_link_explained(src: sa.Connection, invoice_id: UUID, found: InvoiceTransform | None) -> bool:
    """No item-linked line, or every one is a T-ITEM-NONSTOCK application of the transform manifest."""
    ids = _item_line_ids(src, invoice_id)
    return not ids or (found is not None and found.items_explained(ids))


def _items(src: sa.Connection, invoice_id: UUID) -> int:
    """Invoice lines carrying an inventory item (legacy ``item_id``)."""
    table = _reflect(src, "supplier_invoice_line", "ap" if src.dialect.name == "postgresql" else None)
    if "item_id" not in table.c or "invoice_id" not in table.c:
        raise HistoricalEvidenceError("source_column_missing:supplier_invoice_line.item_id")
    return src.execute(
        sa.select(sa.func.count())
        .select_from(table)
        .where(_hex_in(table.c.invoice_id, [invoice_id]), table.c.item_id.is_not(None))
    ).scalar_one()


@dataclass(frozen=True)
class ApprovedInvoiceVerifier:
    source: sa.Connection
    manifest: SnapshotManifest
    evidence_reference: str
    source_owner: str = SOURCE_OWNER
    transforms: tuple[tuple[str, str], ...] = ()
    transform_manifest: TransformManifest | None = None  # the importer's transform manifest (B-3)
    fallback_expense_account: str | None = None  # declared, bound in the plan; the transforms are re-derived with it

    def invoice_transform(self, invoice_id: UUID) -> InvoiceTransform | None:
        return self.transform_manifest.for_invoice(invoice_id) if self.transform_manifest is not None else None

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
            found = self.invoice_transform(invoice_id)
            gate = WaiverGate(
                load_review(connection, tenant, invoice_id, manifest.snapshot_sha256, allowed=RECOGNITION_DECISIONS),
                {**dict(self.transforms), **(found.waivable_checks() if found else {})},
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
            destination.connection(),
            scope.tenant_id,
            invoice_id,
            self.manifest.snapshot_sha256,
            allowed=RECOGNITION_DECISIONS,
        )

    def _evidence(
        self, tenant: UUID, invoice_id: UUID, dest: sa.Connection, gate: WaiverGate
    ) -> ApprovedInvoiceRecognitionEvidence:
        _require(gate.review is None or gate.review.legacy_journal_id is None, "review_journal_unexpected")
        src = self.source
        header = _one(
            _read(src, "supplier_invoice", key="invoice_id", value=invoice_id), "source_invoice_missing_or_duplicate"
        )
        _tenant([header], tenant, "source_invoice", "organization_id")
        _require(header["invoice_type"] == "STANDARD", "source_type")
        _require(header["status"] == NOT_POSTED_STATUS, "source_status_not_approved")
        _require(header["posting_status"] != "POSTED", "source_posting_status_posted")
        _require(header["posting_status"] == NOT_POSTED_POSTING_STATUS, "source_posting_status_not_not_posted")
        _require(header["journal_entry_id"] is None, "source_journal_present")
        _require(header["posted_at"] is None, "source_posted_at_present")
        _require(header["amount_paid"] == 0, "source_paid")
        _require(
            header["withholding_tax_amount"] == 0 and header["withholding_tax_code_id"] is None, "source_withholding"
        )
        _require(header["is_prepayment"] is False and header["prepayment_applied"] == 0, "source_prepayment")
        _require(header["stamp_duty_amount"] == 0, "unsupported_stamp_duty")
        if "source_allocations" in gate.waived and _read(src, "payment_allocation", key="invoice_id", value=invoice_id):
            _allocations_without_control_effect(src, invoice_id, header["ap_control_account_id"])
        gate.require(
            not _read(src, "payment_allocation", key="invoice_id", value=invoice_id),
            "source_allocations",
            "source_allocations",
        )
        for field in ("subtotal", "tax_amount", "total_amount"):
            amount = header[field]
            # Exact remainder, not quantization: source storage must never round.
            _require(isinstance(amount, Decimal) and amount >= 0, "source_amount_scale")
            gate.require(_at_scale(amount, 2), "source_amount_scale", "source_amount_scale")
        _require(
            header["total_amount"] > 0 and _sum([header["subtotal"], header["tax_amount"]]) == header["total_amount"],
            "source_total",
        )
        organization = _one(_read(src, "organization", key="organization_id", value=tenant), "source_organization")
        _require(
            header["currency_code"] == organization["functional_currency_code"]
            and header["exchange_rate"] == 1
            and header["functional_currency_amount"] == header["total_amount"],
            "unsupported_currency",
        )
        # No legacy GL footprint of any kind: journals, journal lines, posted ledger lines.
        journals = _read(src, "journal_entry", key="source_document_id", value=invoice_id)
        # A POSTED legacy VAT-deferral journal is already in the v2 ledger (Dr 1455): posting the invoice under B1
        # would debit the deferral account a second time. A named refusal, never dropped.
        _require(
            not any(
                str(j["source_module"]) == "AP"
                and str(j["source_document_type"]) == DEFERRAL_DOCUMENT_TYPE
                and str(j["status"]).strip().upper() == "POSTED"
                for j in journals
            ),
            "deferral_on_unposted_invoice",
        )
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
                    "currency_code",
                    "exchange_rate",
                    "subtotal",
                    "tax_amount",
                    "total_amount",
                )
            }
        )
        found = rederive_transform(self, tenant, invoice_id, header)
        _match(found.header_values(header) if found else header, target, mapping, "header")
        for name in ("invoice_date", "received_date"):
            # Legacy dates may arrive as date or ISO text; compare as calendar days.
            _require(
                _day(header[name], "source_" + name) == _day(target[name], "destination_" + name), "header_" + name
            )
        lines = _read(src, "supplier_invoice_line", key="invoice_id", value=invoice_id)
        targets = _read(dest, "supplier_invoice_lines", schema="mod_payables", key="invoice_id", value=invoice_id)
        _tenant(targets, tenant, "invoice_line")
        # Goods-receipt, purchase-order, stock and inventory links change the accounting (GRNI,
        # inventory valuation) and are not supported: refuse rather than post the plain consequence.
        _require(
            target["procurement_ref"] is None and target["receipt_evidence_fingerprint"] is None,
            "destination_procurement_ref",
        )
        _require(
            all(row["po_line_id"] is None and row["goods_receipt_line_id"] is None for row in lines),
            "source_procurement_link",
        )
        line_ids = [row["line_id"] for row in lines]
        _require(item_link_explained(src, invoice_id, found), "source_item_link")
        _require(
            _linked_rows(
                src,
                "inventory_transaction",
                "inv",
                {"source_document_id": [invoice_id], "source_document_line_id": line_ids},
            )
            == 0,
            "source_inventory_transaction",
        )
        line_mapping = {k: k for k in ("invoice_id", "line_number", "description", "line_amount", "tax_amount")}
        line_mapping["posting_account_ref"] = "expense_account_id"
        # No legacy GL footprint (checked above), so the snapshot rule applies: B1 split / B2 folded.
        outcome, tax_evidence = invoice_tax(src, tenant, None, lines)
        for original, imported in _pair(lines, targets, "line_id", "invoice_line"):
            _match(found.line_values(original) if found else original, imported, line_mapping, "invoice_line")
            derived = require_line_tax_account(outcome, original["line_id"], imported, found)
            # A folded taxed line is a WHT line keyed as additive tax; it must never be posted as cost. A line the
            # importer transformed (D-M2: WHT removed, landed with no tax) is explained by the transform manifest.
            landed = found.lines.get(canonical(original["line_id"]) or "") if found else None
            _require(
                derived.klass != "B2" or (landed is not None and landed["tax_amount"] == 0),
                "folded_tax_line_requires_review",
            )
        gate.require(
            _sum([row["line_amount"] for row in lines]) == header["subtotal"]
            and _sum([row["tax_amount"] for row in lines]) == header["tax_amount"],
            "invoice_line_totals",
            "line_totals",
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
                **({"transform_manifest": transform_facts(found, self.transform_manifest)} if found else {}),
                tax_accounts=dict(
                    invoice_class=outcome.klass,
                    a1_expense_elsewhere=outcome.expense_elsewhere,
                    lines=[
                        dict(line_id=line_id, klass=item.klass, ref=item.ref)
                        for line_id, item in sorted(outcome.lines.items())
                    ],
                    codes=tax_evidence,
                ),
                **gate.facts(),
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
