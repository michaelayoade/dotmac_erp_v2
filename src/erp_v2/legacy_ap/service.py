"""Enter a legacy NEVER-POSTED open supplier invoice as a NEW ERP draft, with its legacy reference.

Ruling (register v1291): invoices legacy never posted (census R6) are re-entered through
the normal supplier-invoice path (draft, submit, B1 approve and post) carrying a mandatory
legacy reference. Legacy invoices legacy ALREADY posted are never re-entered here: their
expense, tax and AP control are already in the imported legacy journals, so posting again
would double-count; they go through historical admission (payables_history_cli).

Refused: a malformed legacy id, a missing evidence reference or never-posted attestation,
a legacy id already referenced, one the importer already landed (destination invoice id is
the legacy invoice id), and one already admitted historically. The ERP database cannot
prove the legacy posting state (imported journals do not carry the invoice id), so the
attestation is recorded, bound to the operator's census evidence reference.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from dotmac_kernel.models import Party, Tenant
from dotmac_payables.models import HistoricalInvoiceReceipt, SupplierInvoice
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.legacy_ap.models import LegacyInvoiceReference
from erp_v2.web import revenue_services as drafts

NEVER_POSTED = "never_posted"


class LegacyReentryRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _legacy_id(values: Mapping[str, str]) -> UUID:
    raw = values.get("legacy_invoice_id", "")
    try:
        legacy = UUID(raw)
    except ValueError as exc:
        raise LegacyReentryRefused("legacy_id_invalid", "Enter the legacy invoice id as a UUID") from exc
    if str(legacy) != raw:
        raise LegacyReentryRefused("legacy_id_invalid", "Enter the legacy invoice id in lowercase dashed form")
    return legacy


def _preflight(db: Session, tenant_id: UUID, legacy: UUID) -> None:
    if db.scalar(
        select(LegacyInvoiceReference.id).where(
            LegacyInvoiceReference.tenant_id == tenant_id, LegacyInvoiceReference.legacy_invoice_id == legacy
        )
    ):
        raise LegacyReentryRefused("legacy_already_entered", "That legacy invoice was already re-entered")
    if db.scalar(
        select(SupplierInvoice.id).where(SupplierInvoice.tenant_id == tenant_id, SupplierInvoice.id == legacy)
    ):
        raise LegacyReentryRefused(
            "legacy_already_landed", "That legacy invoice was already imported; it cannot be entered again"
        )
    if db.scalar(
        select(HistoricalInvoiceReceipt.id).where(
            HistoricalInvoiceReceipt.tenant_id == tenant_id,
            (HistoricalInvoiceReceipt.invoice_id == legacy)
            | (HistoricalInvoiceReceipt.source_invoice_id == str(legacy)),
        )
    ):
        raise LegacyReentryRefused(
            "legacy_already_admitted", "That legacy invoice was already admitted historically; it cannot be entered"
        )


def create_legacy_reentry_draft(
    db: Session, *, tenant: Tenant, party: Party, values: Mapping[str, str]
) -> SupplierInvoice:
    """One savepoint: the supplier draft and its legacy reference, or nothing."""
    legacy = _legacy_id(values)
    evidence = values.get("evidence_ref", "").strip()
    if not evidence or len(evidence) > 255:
        raise LegacyReentryRefused("evidence_required", "Enter the census or review evidence reference (max 255)")
    if values.get("legacy_posting_state") != NEVER_POSTED:
        raise LegacyReentryRefused(
            "never_posted_attestation_required", "Confirm that legacy never posted this invoice to the ledger"
        )
    if party.tenant_id != tenant.id:
        raise LegacyReentryRefused("actor_tenant", "The acting party must belong to this tenant")
    _preflight(db, tenant.id, legacy)
    clean = {k: v for k, v in values.items() if k not in {"legacy_invoice_id", "evidence_ref", "legacy_posting_state"}}
    try:
        with db.begin_nested():
            invoice = drafts.create_supplier_draft(db, tenant=tenant, party=party, values=clean)
            db.add(
                LegacyInvoiceReference(
                    tenant_id=tenant.id,
                    legacy_invoice_id=legacy,
                    invoice_id=invoice.id,
                    legacy_posting_state=NEVER_POSTED,
                    evidence_ref=evidence,
                    entered_by=str(party.id),
                )
            )
            db.flush()
            return invoice
    except drafts.RevenueActionError as exc:
        raise LegacyReentryRefused("draft_refused", str(exc)) from exc
    except IntegrityError as exc:
        raise LegacyReentryRefused("legacy_already_entered", "That legacy invoice was already re-entered") from exc
