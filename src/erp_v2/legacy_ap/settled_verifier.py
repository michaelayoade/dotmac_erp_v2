"""Trusted, SELECT-only verifier for payables ``admit_historical_settled_invoice`` (a5).

The ERP side of the owner's verifier contract (``HistoricalSettlement`` docstring, review A5-1).
Michael's ruling: the legacy partially paid supplier invoices are migrated into their accounting
settled ledgers. From the restored legacy snapshot, read-only, it proves for one invoice:

* the invoice is legacy POSTED (status POSTED or PARTIALLY_PAID, posting status POSTED), STANDARD,
  functional currency (NGN) at rate 1, with an imported invoice journal (the unpaid-path proof);
* every settlement is exactly one legacy payment allocation (``source_settlement_id`` is the legacy
  allocation id) with no discount and no FX difference, on a payment that is CLEARED (the only
  legacy status that posts to the GL) for the same tenant, supplier and currency;
* each payment's journal is POSTED in the legacy GL (AP, SUPPLIER_PAYMENT, citing the payment, not a
  reversal), its posted ledger lines balance, and its imported destination journal and ledger lines
  exist and equal the legacy ones;
* the payment's allocations sum to its gross amount (net + withholding) and its debit to the AP control
  account equals the allocations posted against that account (the residual cross-check against legacy
  GL 2000: what legacy control still holds for the invoice is exactly total - paid);
* legacy AP control holds nothing else against the invoice: the posted ledger lines on the control
  account whose source document is the invoice are exactly the invoice journal's own lines
  (``invoice_control_other_postings`` otherwise), so the residual is what legacy GL still owes;
* allocation sums equal the invoice paid amount; the residual open amount is total - paid, exactly, in
  two decimals, and positive;
* no goods receipt, purchase order, item or inventory link; legacy settlement ids are unique.

Payment withholding is attributed to the payment's single allocation; a withholding payment that spans
several allocations is refused (``payment_withholding_split_unsupported``), as splitting it would be a
financial decision. Amounts are never recomputed or rounded. Any doubt refuses with a named reason.
The verifier never constructs connections, writes, or commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_payables import (
    HistoricalSettledInvoiceCommand,
    HistoricalSettledInvoiceEvidence,
    HistoricalSettlement,
)
from sqlalchemy.orm import Session

from erp_v2.importer.payables_history import (
    DESTINATION_JOURNAL,
    SOURCE_JOURNAL,
    SOURCE_JOURNAL_REFERENCE,
    SOURCE_LEDGER,
    HistoricalPayablesVerifier,
    WaiverGate,
    _at_scale,
    _day,
    _equal,
    _fingerprint,
    _id,
    _match,
    _one,
    _pair,
    _project,
    _project_rows,
    _read,
    _require,
    _tenant,
    rederive_transform,
)
from erp_v2.importer.payables_reconciliation import _sum
from erp_v2.legacy_ap.recognition_verifier import _linked_rows, item_link_explained

ALLOCATION_FIELDS = [
    "allocation_id",
    "payment_id",
    "invoice_id",
    "allocated_amount",
    "discount_taken",
    "exchange_difference",
    "allocation_date",
]
PAYMENT_FIELDS = [
    "payment_id",
    "organization_id",
    "supplier_id",
    "status",
    "payment_date",
    "currency_code",
    "amount",
    "exchange_rate",
    "functional_currency_amount",
    "gross_amount",
    "withholding_tax_amount",
    "journal_entry_id",
]
POSTED_PAYMENT_STATUS = "CLEARED"
PAYMENT_SOURCE_DOCUMENT = "SUPPLIER_PAYMENT"
ALLOCATION_DOMAIN = "erp.payables.legacy-allocation.v1"
SETTLED_FACTS_DOMAIN = "erp.payables.historical-settled-facts.v1"


def _one_sided(row: dict, suffix: str) -> bool:
    """Non-negative exact money, exactly one of debit/credit positive (as the invoice journal proof)."""
    debit, credit = row["debit_amount" + suffix], row["credit_amount" + suffix]
    return (
        isinstance(debit, Decimal)
        and isinstance(credit, Decimal)
        and debit >= 0
        and credit >= 0
        and ((debit > 0 and credit == 0) or (credit > 0 and debit == 0))
    )


def _two_places_waivable(gate: WaiverGate, value: Decimal) -> None:
    gate.require(value.is_finite() and _at_scale(value, 2), "source_amount_scale", "source_amount_scale")


def _two_places(value: object, reason: str) -> Decimal:
    _require(isinstance(value, Decimal) and value.is_finite() and _at_scale(value, 2), reason)
    return value


@dataclass(frozen=True)
class SettledInvoiceVerifier(HistoricalPayablesVerifier):
    @staticmethod
    def command(evidence: HistoricalSettledInvoiceEvidence) -> HistoricalSettledInvoiceCommand:
        return HistoricalSettledInvoiceCommand(
            source_owner=evidence.source_owner,
            source_invoice_id=evidence.source_invoice_id,
            invoice_id=evidence.invoice_id,
            source_snapshot_sha256=evidence.source_snapshot_sha256,
            evidence_fingerprint=evidence.fingerprint(),
        )

    def verify(
        self, *, scope, command: HistoricalSettledInvoiceCommand, destination: Session
    ) -> HistoricalSettledInvoiceEvidence:
        command.validate()
        _require(command.source_owner == self.source_owner, "command_source_owner")
        _require(command.source_snapshot_sha256 == self.manifest.snapshot_sha256, "command_snapshot")
        _require(command.source_invoice_id == str(command.invoice_id), "command_source_invoice")
        evidence = self.evidence(scope=scope, invoice_id=command.invoice_id, destination=destination)
        _require(command.evidence_fingerprint == evidence.fingerprint(), "command_fingerprint")
        return evidence

    def _evidence(
        self, tenant: UUID, invoice_id: UUID, dest: sa.Connection, gate: WaiverGate
    ) -> HistoricalSettledInvoiceEvidence:
        src = self.source
        header, common = self._parts(tenant, invoice_id, dest, gate, settled=True)
        # Goods-receipt and purchase-order links are refused by the shared proof (destination and source
        # lines). Item and inventory links change the accounting too (inventory valuation) and settled
        # admission produces no consequence, so they are refused as in B1b.
        lines = _read(src, "supplier_invoice_line", key="invoice_id", value=invoice_id)
        _require(
            item_link_explained(src, invoice_id, rederive_transform(self, tenant, invoice_id, header)),
            "source_item_link",
        )
        _require(
            _linked_rows(
                src,
                "inventory_transaction",
                "inv",
                {"source_document_id": [invoice_id], "source_document_line_id": [r["line_id"] for r in lines]},
            )
            == 0,
            "source_inventory_transaction",
        )
        self._control_postings(tenant, header)
        allocations = _read(src, "payment_allocation", key="invoice_id", value=invoice_id, fields=ALLOCATION_FIELDS)
        _require(bool(allocations), "source_allocations_missing")
        ids = [_id(row["allocation_id"]) for row in allocations]
        _require(len(set(ids)) == len(ids), "settlement_id_duplicate")
        _require(
            len({_id(row["payment_id"]) for row in allocations}) == len(allocations), "settlement_payment_duplicate"
        )
        payments: dict[str, dict] = {}
        settlements = []
        payment_facts = []
        for allocation in sorted(allocations, key=lambda row: _id(row["allocation_id"])):
            applied = _two_places(allocation["allocated_amount"], "allocation_amount_scale")
            _require(applied > 0, "allocation_amount_scale")
            _require(
                allocation["discount_taken"] == 0 and allocation["exchange_difference"] == 0,
                "allocation_discount_or_fx",
            )
            _require(_equal(allocation["invoice_id"], invoice_id), "allocation_invoice")
            payment_id = _id(allocation["payment_id"])
            if payment_id not in payments:
                payments[payment_id] = self._payment(tenant, header, allocation["payment_id"], dest)
                payment_facts.append(payments[payment_id]["facts"])
            payment = payments[payment_id]
            _require(_day(allocation["allocation_date"], "allocation_date") is not None, "allocation_date")
            settlements.append(
                HistoricalSettlement(
                    source_settlement_id=_id(allocation["allocation_id"]),
                    payment_ref=payment_id,
                    settled_on=_day(allocation["allocation_date"], "allocation_date"),
                    applied_amount=applied,
                    withholding_amount=payment["withholding"],
                    currency_code=header["currency_code"],
                    exchange_rate=Decimal(1),
                    source_row_fingerprint=_fingerprint(ALLOCATION_DOMAIN, _project(allocation, ALLOCATION_FIELDS)),
                    source_journal_ref=payment["journal_ref"],
                    destination_journal_ref=payment["journal_ref"],
                )
            )
        applied_total = _sum([item.applied_amount for item in settlements])
        _require(isinstance(header["amount_paid"], Decimal) and header["amount_paid"] > 0, "source_amount_scale")
        _two_places_waivable(gate, header["amount_paid"])
        paid = header["amount_paid"]
        total = common["total_amount"]
        _require(applied_total == paid, "allocation_sum_mismatch")
        _require(applied_total <= total, "allocation_exceeds_total")
        residual = _sum([total, -paid])
        _require(_at_scale(residual, 2), "residual_scale")
        _require(residual > 0, "invoice_not_partially_paid")
        facts = _fingerprint(
            SETTLED_FACTS_DOMAIN,
            dict(base=common["facts_fingerprint"], payments=sorted(payment_facts, key=lambda row: row["payment_id"])),
        )
        return HistoricalSettledInvoiceEvidence(
            source_status="POSTED",
            settlements=tuple(settlements),
            settled_amount=applied_total,
            residual_amount=residual,
            withholding_tax_amount=_sum([item.withholding_amount for item in settlements]),
            **(common | dict(facts_fingerprint=facts)),
        )

    def _control_postings(self, tenant: UUID, header: dict) -> None:
        """The residual is only provable if legacy AP control holds nothing else against this invoice:
        every posted ledger line on the control account citing the invoice must be one of the invoice
        journal's own lines (a debit note or adjustment citing the invoice refuses)."""
        src, control = self.source, header["ap_control_account_id"]
        cited = [
            r
            for r in _read(src, "posted_ledger_line", key="source_document_id", value=header["invoice_id"])
            if _equal(r["account_id"], control)
        ]
        _tenant(cited, tenant, "invoice_control_posting", "organization_id")
        own = {
            _id(r["ledger_line_id"])
            for r in _read(src, "posted_ledger_line", key="journal_entry_id", value=header["journal_entry_id"])
            if _equal(r["account_id"], control)
        }
        _require(
            bool(own) and {_id(r["ledger_line_id"]) for r in cited} == own,
            "invoice_control_other_postings",
        )

    def _payment(self, tenant: UUID, header: dict, payment_id: object, dest: sa.Connection) -> dict:
        src = self.source
        payment = _one(
            _read(src, "supplier_payment", key="payment_id", value=payment_id, fields=PAYMENT_FIELDS),
            "settlement_payment_missing",
        )
        _tenant([payment], tenant, "source_payment", "organization_id")
        _require(payment["status"] == POSTED_PAYMENT_STATUS, "payment_not_posted")
        _require(_equal(payment["supplier_id"], header["supplier_id"]), "payment_supplier_mismatch")
        _require(payment["currency_code"] == header["currency_code"], "unsupported_currency")
        amount = _two_places(payment["amount"], "payment_amount_scale")
        withholding = _two_places(payment["withholding_tax_amount"], "payment_amount_scale")
        _require(amount > 0 and withholding >= 0, "payment_amount_scale")
        _require(
            payment["exchange_rate"] in (None, Decimal(1)) and payment["functional_currency_amount"] == amount,
            "unsupported_currency",
        )
        gross = _sum([amount, withholding])
        _require(payment["gross_amount"] in (None, gross), "payment_gross_mismatch")
        _require(payment["journal_entry_id"] is not None, "payment_journal_missing")
        pid = _id(payment["payment_id"])
        allocations = _read(src, "payment_allocation", key="payment_id", value=payment_id, fields=ALLOCATION_FIELDS)
        _require(
            _sum([_two_places(a["allocated_amount"], "allocation_amount_scale") for a in allocations]) == gross,
            "payment_allocation_sum_mismatch",
        )
        _require(withholding == 0 or len(allocations) == 1, "payment_withholding_split_unsupported")
        control = header["ap_control_account_id"]
        expected_control = Decimal(0)
        for item in allocations:
            if _equal(item["invoice_id"], header["invoice_id"]):
                other = header
            else:
                other = _one(
                    _read(src, "supplier_invoice", key="invoice_id", value=item["invoice_id"]),
                    "payment_allocation_invoice_missing",
                )
                _tenant([other], tenant, "payment_allocation_invoice", "organization_id")
            if _equal(other["ap_control_account_id"], control):
                expected_control = _sum([expected_control, item["allocated_amount"]])
        journal, ledger, target, posted = self._payment_journal(tenant, header, payment, dest)
        debits = [r["debit_amount"] for r in ledger if _equal(r["account_id"], control)]
        credits = [r["credit_amount"] for r in ledger if _equal(r["account_id"], control)]
        _require(_sum(credits) == 0 and _sum(debits) == expected_control, "payment_gl_control_mismatch")
        facts = dict(
            payment_id=pid,
            payment=_project(payment, PAYMENT_FIELDS),
            source_journal=_project(journal, SOURCE_JOURNAL),
            destination_journal=_project(target, DESTINATION_JOURNAL),
            source_ledger=_project_rows(ledger, SOURCE_LEDGER, "ledger_line_id"),
            destination_ledger=sorted(
                [dict(id=_id(row["id"]), debit=row["debit"], credit=row["credit"]) for row in posted],
                key=lambda row: row["id"],
            ),
        )
        return dict(withholding=withholding, journal_ref=_id(payment["journal_entry_id"]), facts=facts)

    def _payment_journal(self, tenant: UUID, header: dict, payment: dict, dest: sa.Connection):
        src = self.source
        jid, pid = payment["journal_entry_id"], _id(payment["payment_id"])
        journal = _one(_read(src, "journal_entry", key="journal_entry_id", value=jid), "payment_journal_missing")
        _tenant([journal], tenant, "payment_journal", "organization_id")
        _require(journal["status"] == "POSTED", "payment_journal_not_posted")
        _require(
            journal["source_module"] == "AP"
            and journal["source_document_type"] == PAYMENT_SOURCE_DOCUMENT
            and _equal(journal["source_document_id"], pid),
            "payment_journal_owner",
        )
        _require(
            journal["is_reversal"] is False
            and journal["journal_type"] != "REVERSAL"
            and journal["reversed_journal_id"] is None
            and journal["reversal_journal_id"] is None,
            "payment_journal_reversed",
        )
        _require(
            journal["currency_code"] == header["currency_code"] and journal["exchange_rate"] == 1,
            "unsupported_currency",
        )
        ledger = _read(src, "posted_ledger_line", key="journal_entry_id", value=jid)
        _tenant(ledger, tenant, "payment_source_ledger", "organization_id")
        _require(bool(ledger), "payment_journal_not_posted")
        _require(
            all(
                r["source_module"] == "AP"
                and r["source_document_type"] == PAYMENT_SOURCE_DOCUMENT
                and _equal(r["source_document_id"], pid)
                for r in ledger
            ),
            "payment_ledger_source",
        )
        debit = _sum([r["debit_amount"] for r in ledger])
        _require(
            debit == _sum([r["credit_amount"] for r in ledger]) == journal["total_debit"] == journal["total_credit"],
            "payment_ledger_unbalanced",
        )
        target = _one(
            _read(dest, "journal_entries", schema="mod_accounting", key="id", value=jid),
            "payment_destination_journal_missing",
        )
        _tenant([target], tenant, "payment_destination_journal")
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
        _match(journal, target, mapping, "payment_journal")
        _require(
            target["source_document_id"] == f"{SOURCE_JOURNAL_REFERENCE}{journal['journal_entry_id']}",
            "payment_journal_source_document_id",
        )
        self._provenance(target, "payment_journal")
        # The same posting stamp the invoice journal proof requires.
        stamp = (
            f"{SOURCE_JOURNAL_REFERENCE}{target['id']}@{_day(journal['posting_date'], 'payment_journal_posting_date')}"
        )
        _require(target["approval_reference"] == stamp, "payment_journal_posting_stamp")
        originals = _read(src, "journal_entry_line", key="journal_entry_id", value=jid)
        imported = _read(dest, "journal_lines", schema="mod_accounting", key="journal_id", value=jid)
        _tenant(imported, tenant, "payment_journal_line")
        line_map = {key: key for key in ("line_number", "account_id", "description", "created_at")}
        line_map.update(
            journal_id="journal_entry_id",
            debit="debit_amount",
            credit="credit_amount",
            debit_functional="debit_amount_functional",
            credit_functional="credit_amount_functional",
        )
        for original, landed in _pair(originals, imported, "line_id", "payment_journal_line"):
            _require(_one_sided(original, ""), "payment_journal_line_amounts")
            _match(original, landed, line_map, "payment_journal_line")
            _require(
                original["currency_code"] in (None, header["currency_code"])
                and original["exchange_rate"] in (None, Decimal(1))
                and original["debit_amount"] == original["debit_amount_functional"]
                and original["credit_amount"] == original["credit_amount_functional"],
                "unsupported_currency",
            )
        for side in ("debit", "credit"):
            _require(
                _sum([row[side + "_amount"] for row in originals])
                == journal["total_" + side]
                == journal["total_" + side + "_functional"],
                "payment_journal_totals",
            )
        _require(all(_one_sided(row, "") for row in ledger), "payment_ledger_line_amounts")
        posted = _read(dest, "posted_ledger_lines", schema="mod_accounting", key="journal_id", value=jid)
        _tenant(posted, tenant, "payment_destination_ledger")
        ledger_map = {key: key for key in ("account_id", "entry_date", "posting_date", "posted_at")}
        ledger_map.update(
            journal_id="journal_entry_id",
            debit="debit_amount",
            credit="credit_amount",
            posted_by="posted_by_user_id",
        )
        for original, landed in _pair(ledger, posted, "ledger_line_id", "payment_ledger"):
            _match(original, landed, ledger_map, "payment_ledger")
            self._provenance(landed, "payment_ledger")
        return journal, ledger, target, posted


__all__ = ["SettledInvoiceVerifier"]
