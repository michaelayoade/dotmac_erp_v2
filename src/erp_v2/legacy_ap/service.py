"""Enter a legacy NEVER-POSTED open supplier invoice as a NEW ERP draft, with its legacy reference.

Ruling (register v1291): invoices legacy never posted (census R4/R6) are re-entered through
the normal supplier-invoice path (draft, submit, B1 approve and post) carrying a legacy
reference. Legacy invoices legacy ALREADY posted are never re-entered here: their expense,
tax and AP control are already in the imported legacy journals, so posting again would
double-count; they go through historical admission (payables_history_cli).

Eligibility is a census fact, not a UI assertion: the operator loads a sha256-bound list of
legacy invoice ids the flip-window census classed never-posted (``cohort_cli``). Until a
cohort is loaded for the tenant, re-entry is OFF. An id not on the list is refused, and the
reference records the cohort sha as its evidence.

Also refused: a malformed legacy id, an id already referenced by a non-VOID invoice, one the
importer already landed (destination invoice id is the legacy invoice id), and one already
admitted historically. A voided re-entry releases its legacy id: a new attempt is allowed only
when every earlier attempt's invoice is VOID (the earlier rows are kept).
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from dotmac_kernel.models import Party, Tenant
from dotmac_payables.contracts import InvoiceStatus
from dotmac_payables.models import HistoricalInvoiceReceipt, SupplierInvoice
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.legacy_ap.models import LegacyInvoiceReference, LegacyNeverPostedCohort, LegacyNeverPostedMember
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


def active_cohort(db: Session, tenant_id: UUID) -> LegacyNeverPostedCohort | None:
    return db.scalar(select(LegacyNeverPostedCohort).where(LegacyNeverPostedCohort.tenant_id == tenant_id))


def _preflight(db: Session, tenant_id: UUID, legacy: UUID, cohort: LegacyNeverPostedCohort) -> int:
    """Return the next attempt number, or refuse."""
    on_list = db.scalar(
        select(LegacyNeverPostedMember.legacy_invoice_id).where(
            LegacyNeverPostedMember.tenant_id == tenant_id,
            LegacyNeverPostedMember.cohort_id == cohort.id,
            LegacyNeverPostedMember.legacy_invoice_id == legacy,
        )
    )
    if on_list is None:
        raise LegacyReentryRefused(
            "not_in_never_posted_cohort", "That legacy invoice is not on the census never-posted list"
        )
    earlier = list(
        db.execute(
            select(LegacyInvoiceReference.attempt, SupplierInvoice.status)
            .outerjoin(
                SupplierInvoice,
                (SupplierInvoice.tenant_id == LegacyInvoiceReference.tenant_id)
                & (SupplierInvoice.id == LegacyInvoiceReference.invoice_id),
            )
            .where(LegacyInvoiceReference.tenant_id == tenant_id, LegacyInvoiceReference.legacy_invoice_id == legacy)
        )
    )
    if any(status != InvoiceStatus.VOID for _attempt, status in earlier):
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
    return max((attempt for attempt, _status in earlier), default=0) + 1


def create_legacy_reentry_draft(
    db: Session, *, tenant: Tenant, party: Party, values: Mapping[str, str]
) -> SupplierInvoice:
    """One savepoint: the supplier draft and its legacy reference, or nothing."""
    cohort = active_cohort(db, tenant.id)
    if cohort is None:
        raise LegacyReentryRefused("reentry_not_enabled", "Legacy re-entry is off: no never-posted list is loaded")
    legacy = _legacy_id(values)
    if party.tenant_id != tenant.id:
        raise LegacyReentryRefused("actor_tenant", "The acting party must belong to this tenant")
    attempt = _preflight(db, tenant.id, legacy, cohort)
    clean = {k: v for k, v in values.items() if k != "legacy_invoice_id"}
    try:
        with db.begin_nested():
            invoice = drafts.create_supplier_draft(db, tenant=tenant, party=party, values=clean)
            db.add(
                LegacyInvoiceReference(
                    tenant_id=tenant.id,
                    legacy_invoice_id=legacy,
                    attempt=attempt,
                    invoice_id=invoice.id,
                    legacy_posting_state=NEVER_POSTED,
                    evidence_ref=cohort.cohort_sha256,
                    entered_by=str(party.id),
                )
            )
            db.flush()
            return invoice
    except drafts.RevenueActionError as exc:
        raise LegacyReentryRefused("draft_refused", str(exc)) from exc
    except IntegrityError as exc:
        raise LegacyReentryRefused("legacy_already_entered", "That legacy invoice was already re-entered") from exc
