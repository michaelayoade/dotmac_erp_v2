"""Approve a submitted supplier invoice and post it to the AP control account (B1).

One transaction (one SAVEPOINT inside the caller's request transaction):

    payables.approve_invoice          -> obligations + liability event
    payables.build_invoice_accounting_consequence
    erp_v2.posting.post_consequence   -> Dr expense/tax, Cr liability account (AP control)
    payables.record_accounting_receipt

Either all four happen or none do: a refusal anywhere rolls the savepoint back, so
there is never a liability without a ledger entry or a ledger entry without a
liability. Payables does not enforce approver != submitter, so this module does.
Approving an already approved and posted invoice is a no-op replay. Invoices that
carry dimension refs are refused (the bridge does not map dimensions); they are
never posted with the dimensions silently dropped. No approvals-module request is
consulted: the approval reference is the party-asserted ``erp.b1`` string below.
Void/credit-note paths are out of scope (W18).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from dotmac_kernel.models import Party, Tenant, UserCredential
from dotmac_payables import service as payables
from dotmac_payables.contracts import AccountingReceiptInput, InvoiceStatus, PayablesError
from dotmac_payables.models import AccountingReceipt, HistoricalInvoiceReceipt, SupplierInvoice, SupplierInvoiceLine
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.governance import policy as governance
from erp_v2.ledger_roles import invoice_account_violation
from erp_v2.posting.bridge import post_consequence
from erp_v2.posting.rules import PostingRefused

AUDIT_APPROVED_POSTED = "erp.finance.supplier_invoice.approved_posted"
AUDIT_APPROVAL_REFUSED = "erp.finance.supplier_invoice.approval_refused"
DOCUMENT_KIND = "supplier_invoice"


class ApprovalRefused(ValueError):
    """A safe, user-facing refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    invoice_id: UUID
    accounting_reference: str
    replayed: bool


def _audit(db: Session, *, tenant: Tenant, party: Party, invoice_id: UUID, action: str, success: bool, details) -> None:
    write_audit_event(
        db,
        tenant_id=tenant.id,
        actor_party_id=party.id,
        actor_type="user",
        actor_id=str(party.id),
        actor_label=getattr(party, "email", None) or None,
        source_application="dotmac_erp",
        action=action,
        entity_type="supplier_invoice",
        entity_id=str(invoice_id),
        is_success=success,
        details=details,
    )


def _submitter_party_id(db: Session, tenant_id: UUID, submitted_by: str) -> UUID:
    """Resolve the recorded submitter to a tenant Party id, or refuse.

    Imported invoices carry the legacy UserCredential id, native ones the Party id.
    """
    try:
        raw = UUID(submitted_by)
    except ValueError:
        raw = None
    if raw is not None:
        if db.scalar(select(Party.id).where(Party.tenant_id == tenant_id, Party.id == raw)) is not None:
            return raw
        credential_party = db.scalar(
            select(UserCredential.party_id).where(UserCredential.tenant_id == tenant_id, UserCredential.id == raw)
        )
        if credential_party is not None:
            return credential_party
    raise ApprovalRefused(
        "submitter_unresolved", "The submitter cannot be resolved to a person, so separation of duties cannot hold"
    )


def _guarded(db: Session, *, tenant: Tenant, party: Party, invoice_id: UUID, now: datetime) -> ApprovalOutcome:
    scope = TenantScope(tenant.id)
    actor = str(party.id)
    invoice = db.scalar(
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tenant.id, SupplierInvoice.id == invoice_id)
        .with_for_update()
    )
    if invoice is None:
        raise ApprovalRefused("not_found", "Supplier invoice was not found")
    receipt = db.scalar(
        select(AccountingReceipt).where(
            AccountingReceipt.tenant_id == tenant.id,
            AccountingReceipt.document_kind == DOCUMENT_KIND,
            AccountingReceipt.document_id == invoice.id,
        )
    )
    if db.scalar(
        select(HistoricalInvoiceReceipt.id).where(
            HistoricalInvoiceReceipt.tenant_id == tenant.id, HistoricalInvoiceReceipt.invoice_id == invoice.id
        )
    ):
        raise ApprovalRefused("historical_invoice", "Historical imported invoices are not approved or posted here")
    if invoice.status == InvoiceStatus.APPROVED:
        if receipt is None:
            raise ApprovalRefused(
                "approved_without_ledger_receipt",
                "Invoice is approved but has no ledger receipt; refer it to the exceptions review",
            )
        return ApprovalOutcome(invoice.id, receipt.accounting_reference, True)
    if invoice.status != InvoiceStatus.SUBMITTED:
        raise ApprovalRefused("not_submitted", "Only a submitted supplier invoice can be approved")
    if receipt is not None:
        raise ApprovalRefused("receipt_exists", "A submitted invoice already has a ledger receipt; refer it for review")
    if governance.dual_control(db, tenant.id):
        if not invoice.submitted_by:
            raise ApprovalRefused(
                "submitter_unknown", "The submitter is not recorded, so separation of duties cannot hold"
            )
        if _submitter_party_id(db, tenant.id, invoice.submitted_by) == party.id:
            raise ApprovalRefused(
                "sod_submitter_is_approver", "You submitted this invoice; another person must approve it"
            )
    lines = list(
        db.scalars(
            select(SupplierInvoiceLine).where(
                SupplierInvoiceLine.tenant_id == tenant.id, SupplierInvoiceLine.invoice_id == invoice.id
            )
        )
    )
    with_dimensions = sorted(line.line_number for line in lines if line.dimension_refs)
    if with_dimensions:
        raise ApprovalRefused(
            "dimensions_unsupported",
            "Lines " + ", ".join(map(str, with_dimensions)) + " carry cost-centre/project/segment dimensions, "
            "which cannot be posted yet; refer this invoice to the exceptions review",
        )
    violation = invoice_account_violation(
        db,
        tenant.id,
        liability_ref=invoice.liability_account_ref,
        lines=((line.posting_account_ref, line.tax_amount, line.tax_account_ref) for line in lines),
    )
    if violation is not None:
        raise ApprovalRefused(violation[0], violation[1])
    approval_ref = f"erp.b1:supplier_invoice:{invoice.id}:approver:{actor}"
    payables.approve_invoice(
        db,
        scope=scope,
        invoice_id=invoice.id,
        approval_reference=approval_ref,
        approved_by=actor,
        idempotency_key=f"erp.b1.approve:{invoice.id}",
        idempotency_expires_at=None,
        approved_at=now,
    )
    consequence = payables.build_invoice_accounting_consequence(db, tenant_id=tenant.id, invoice_id=invoice.id)
    posted = post_consequence(db, scope=scope, consequence=consequence, approval_ref=approval_ref, actor=actor, now=now)
    payables.record_accounting_receipt(
        db,
        scope=scope,
        command=AccountingReceiptInput(
            document_kind=DOCUMENT_KIND,
            document_id=invoice.id,
            consequence_fingerprint=consequence.fingerprint,
            accounting_reference=str(posted.journal_id),
            accounting_evidence_fingerprint=posted.evidence_fingerprint,
        ),
        idempotency_key=f"erp.b1.receipt:{invoice.id}",
        idempotency_expires_at=None,
        recorded_at=now,
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        invoice_id=invoice.id,
        action=AUDIT_APPROVED_POSTED,
        success=True,
        details={
            "journal_id": str(posted.journal_id),
            "journal_number": posted.journal_number,
            "consequence_fingerprint": consequence.fingerprint,
            "submitted_by": invoice.submitted_by,
        },
    )
    return ApprovalOutcome(invoice.id, str(posted.journal_id), posted.replayed)


def approve_and_post(
    db: Session, *, tenant: Tenant, party: Party, invoice_id: UUID, now: datetime | None = None
) -> ApprovalOutcome:
    """Approve + post in one savepoint, or refuse leaving no invoice/ledger change."""
    if party.tenant_id != tenant.id:
        raise ApprovalRefused("actor_tenant", "The acting party must belong to this tenant")
    moment = now or datetime.now(UTC)
    try:
        with db.begin_nested():
            return _guarded(db, tenant=tenant, party=party, invoice_id=invoice_id, now=moment)
    except ApprovalRefused as exc:
        refusal = exc
    except PostingRefused as exc:
        refusal = ApprovalRefused(f"posting_{exc.code}", f"The ledger refused the posting ({exc.code})")
    except (PayablesError, IdempotencyConflict) as exc:
        refusal = ApprovalRefused("payables_refused", str(exc))
    except IntegrityError:
        refusal = ApprovalRefused("conflict", "A concurrent change prevented the approval; reload and retry")
    if refusal.code != "not_found":
        # The savepoint rolled back; record the refusal in the caller's transaction.
        _audit(
            db,
            tenant=tenant,
            party=party,
            invoice_id=invoice_id,
            action=AUDIT_APPROVAL_REFUSED,
            success=False,
            details={"code": refusal.code},
        )
    raise refusal
