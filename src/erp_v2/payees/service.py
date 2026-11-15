"""Governed commands for payee bank instructions (flush-only; the caller owns the transaction).

Lifecycle: proposed -> approved -> superseded | revoked, and proposed -> rejected.

- propose: a named person records a new instruction with mandatory evidence.
- approve: a DIFFERENT person approves. Proposer == approver is refused here and by a DB
  CHECK. Roles are enforced one layer up (permissions + role SoD pairs): treasury can
  never hold the approve permission. Approving supersedes the payee's previous approved
  instruction in the same transaction, so there is never more than one active destination.
- reject / revoke: finance_approver-only fail-safe actions; both need a reason.
- Legacy imports arrive as ``proposed`` with no proposer (origin ``legacy_import``) and
  are approved one at a time like any other proposal. There is no bulk command.

Account numbers are PII: nothing here logs, audits or raises with one. Views expose only
a masked form; ``reveal_account_number`` and ``destination_for_payment`` are the only
unmasked reads, the first is audited per call and needs a reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp_v2.payees.models import InstructionOrigin, InstructionStatus, PayeeBankInstruction, PayeeKind

AUDIT_PROPOSED: Final = "erp.payees.bank_details.proposed"
AUDIT_APPROVED: Final = "erp.payees.bank_details.approved"
AUDIT_REJECTED: Final = "erp.payees.bank_details.rejected"
AUDIT_REVOKED: Final = "erp.payees.bank_details.revoked"
AUDIT_UNMASKED_READ: Final = "erp.payees.bank_details.unmasked_read"
AUDIT_MIGRATED: Final = "erp.payees.bank_details.migrated"
AUDIT_ACTIONS: Final = (
    AUDIT_PROPOSED,
    AUDIT_APPROVED,
    AUDIT_REJECTED,
    AUDIT_REVOKED,
    AUDIT_UNMASKED_READ,
    AUDIT_MIGRATED,
)

SOURCE_APPLICATION: Final = "dotmac_erp"
_BANK_CODE = re.compile(r"^[0-9]{3,6}$")
_NUBAN = re.compile(r"^[0-9]{10}$")


class PayeeBankError(ValueError):
    """A refused command. ``code`` is stable; the message never carries an account number."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class PayeeBankView:
    """Masked read model. Carries no account number."""

    id: UUID
    payee_kind: str
    payee_ref: str
    payee_label: str
    bank_code: str | None
    bank_name: str | None
    account_masked: str
    account_name: str | None
    status: str
    origin: str
    evidence_ref: str | None
    proposed_by_party_id: UUID | None
    proposed_at: datetime
    decided_by_party_id: UUID | None
    decided_at: datetime | None
    approved_at: datetime | None
    shared_account_payees: int = 0
    system_name: str | None = None  # name from the system of record; None = unknown payee


@dataclass(frozen=True)
class PaymentDestination:
    """Unmasked destination for B3/B5 file generation only. The number is hidden from repr."""

    instruction_id: UUID
    bank_code: str
    account_number: str = field(repr=False)
    account_name: str
    payee_kind: str
    payee_ref: str

    def __repr__(self) -> str:
        return f"PaymentDestination({self.instruction_id}, {self.payee_kind}, ****{self.account_number[-4:]})"


def resolve_payee(db: Session, tenant_id: UUID, payee_kind: str, payee_ref: str) -> str | None:
    """System-of-record name for a payee inside the tenant, or None when unknown.

    Employee: ``mod_people.employees`` by canonical dashed UUID (code plus person name).
    Supplier: the payables supplier reference set (``supplier_invoices.supplier_ref`` with the
    latest name snapshot); v2 has no separate supplier master. Never free text.
    """

    if payee_kind == PayeeKind.EMPLOYEE.value:
        from dotmac_kernel.models import PartyPerson
        from dotmac_people.models import Employee

        try:
            employee_id = UUID(payee_ref)
        except ValueError:
            return None
        if str(employee_id) != payee_ref:
            return None
        found = db.execute(
            select(Employee.employee_code, PartyPerson.first_name, PartyPerson.last_name)
            .join(PartyPerson, PartyPerson.party_id == Employee.party_id, isouter=True)
            .where(Employee.tenant_id == tenant_id, Employee.id == employee_id)
        ).first()
        if found is None:
            return None
        return " ".join(part for part in found if part)
    if payee_kind == PayeeKind.SUPPLIER.value:
        from dotmac_payables.models import SupplierInvoice

        return db.scalar(
            select(SupplierInvoice.supplier_name_snapshot)
            .where(SupplierInvoice.tenant_id == tenant_id, SupplierInvoice.supplier_ref == payee_ref)
            .order_by(SupplierInvoice.created_at.desc())
            .limit(1)
        )
    return None


def mask_account(account_number: str) -> str:
    tail = account_number[-4:] if len(account_number) >= 4 else ""
    return f"******{tail}" if tail else "******"


def _now() -> datetime:
    return datetime.now(UTC)


def _text(value: str | None, field_name: str, *, limit: int, required: bool = True) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        if required:
            raise PayeeBankError("invalid_input", f"{field_name} is required")
        return None
    if len(cleaned) > limit:
        raise PayeeBankError("invalid_input", f"{field_name} is too long")
    return cleaned


def missing_fields(bank_code: str | None, account_number: str | None, account_name: str | None) -> list[str]:
    """Names (never values) of fields that fail the strict usability rules."""

    missing = []
    if not bank_code or not _BANK_CODE.match(bank_code):
        missing.append("bank_code")
    if not _NUBAN.match(account_number or ""):
        missing.append("account_number")
    if not (account_name or "").strip():
        missing.append("account_name")
    return missing


def missing_for_approval(row: PayeeBankInstruction) -> list[str]:
    return missing_fields(row.bank_code, row.account_number, row.account_name)


def _audit(db: Session, *, tenant_id: UUID, actor: UUID, action: str, row: PayeeBankInstruction, **details) -> None:
    from dotmac_kernel.audit import write_audit_event

    write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_party_id=actor,
        actor_type="user",
        actor_id=str(actor),
        source_application=SOURCE_APPLICATION,
        action=action,
        entity_type="payee_bank_instruction",
        entity_id=str(row.id),
        is_success=True,
        details={
            "payee_kind": row.payee_kind,
            "payee_ref": row.payee_ref,
            "account_last4": row.account_last4,
            **details,
        },
    )


def _locked(db: Session, tenant_id: UUID, instruction_id: UUID) -> PayeeBankInstruction:
    row = db.scalar(
        select(PayeeBankInstruction)
        .where(PayeeBankInstruction.tenant_id == tenant_id, PayeeBankInstruction.id == instruction_id)
        .with_for_update()
    )
    if row is None:
        raise PayeeBankError("not_found", "instruction not found")
    return row


def _shared_count(db: Session, row: PayeeBankInstruction) -> int:
    """Other payees with a live (proposed/approved) instruction on the same account: a fraud signal."""

    return int(
        db.scalar(
            select(func.count(func.distinct(PayeeBankInstruction.payee_ref))).where(
                PayeeBankInstruction.tenant_id == row.tenant_id,
                PayeeBankInstruction.account_number == row.account_number,
                PayeeBankInstruction.bank_code == row.bank_code,
                PayeeBankInstruction.status.in_((InstructionStatus.PROPOSED.value, InstructionStatus.APPROVED.value)),
                (PayeeBankInstruction.payee_kind != row.payee_kind) | (PayeeBankInstruction.payee_ref != row.payee_ref),
            )
        )
        or 0
    )


def view(db: Session, row: PayeeBankInstruction) -> PayeeBankView:
    return PayeeBankView(
        id=row.id,
        payee_kind=row.payee_kind,
        payee_ref=row.payee_ref,
        payee_label=row.payee_label,
        bank_code=row.bank_code,
        bank_name=row.bank_name,
        account_masked=mask_account(row.account_number),
        account_name=row.account_name,
        status=row.status,
        origin=row.origin,
        evidence_ref=row.evidence_ref,
        proposed_by_party_id=row.proposed_by_party_id,
        proposed_at=row.proposed_at,
        decided_by_party_id=row.decided_by_party_id,
        decided_at=row.decided_at,
        approved_at=row.approved_at,
        shared_account_payees=_shared_count(db, row),
        system_name=resolve_payee(db, row.tenant_id, row.payee_kind, row.payee_ref),
    )


def propose(
    db: Session,
    *,
    tenant_id: UUID,
    proposer_party_id: UUID,
    payee_kind: str,
    payee_ref: str,
    bank_code: str,
    bank_name: str,
    account_number: str,
    account_name: str,
    evidence_ref: str,
    now: datetime | None = None,
) -> PayeeBankInstruction:
    try:
        kind = PayeeKind(payee_kind)
    except ValueError as exc:
        raise PayeeBankError("invalid_input", "payee_kind must be supplier or employee") from exc
    ref = _text(payee_ref, "payee_ref", limit=255)
    system_name = resolve_payee(db, tenant_id, kind.value, ref or "")
    if system_name is None:
        raise PayeeBankError("unknown_payee", "payee is not a known supplier or employee in this tenant")
    label = system_name[:200]
    code = _text(bank_code, "bank_code", limit=10)
    if not _BANK_CODE.match(code or ""):
        raise PayeeBankError("invalid_input", "bank_code must be 3 to 6 digits")
    number = (account_number or "").strip()
    if not _NUBAN.match(number):
        raise PayeeBankError("invalid_input", "account_number must be a 10 digit NUBAN")
    existing_approved = db.scalar(
        select(PayeeBankInstruction).where(
            PayeeBankInstruction.tenant_id == tenant_id,
            PayeeBankInstruction.payee_kind == kind.value,
            PayeeBankInstruction.payee_ref == ref,
            PayeeBankInstruction.status == InstructionStatus.APPROVED.value,
        )
    )
    if existing_approved is not None and (existing_approved.bank_code, existing_approved.account_number) == (
        code,
        number,
    ):
        raise PayeeBankError("unchanged", "the approved instruction already has these bank details")
    row = PayeeBankInstruction(
        id=uuid4(),
        tenant_id=tenant_id,
        payee_kind=kind.value,
        payee_ref=ref,
        payee_label=label,
        bank_code=code,
        bank_name=_text(bank_name, "bank_name", limit=120),
        account_number=number,
        account_last4=number[-4:],
        account_name=_text(account_name, "account_name", limit=200),
        status=InstructionStatus.PROPOSED.value,
        origin=InstructionOrigin.MANUAL.value,
        evidence_ref=_text(evidence_ref, "evidence_ref", limit=240),
        proposed_by_party_id=proposer_party_id,
        proposed_at=now or _now(),
    )
    open_proposal = db.scalar(
        select(PayeeBankInstruction)
        .where(
            PayeeBankInstruction.tenant_id == tenant_id,
            PayeeBankInstruction.payee_kind == kind.value,
            PayeeBankInstruction.payee_ref == ref,
            PayeeBankInstruction.status == InstructionStatus.PROPOSED.value,
        )
        .with_for_update()
    )
    if open_proposal is not None:
        if open_proposal.origin != InstructionOrigin.LEGACY_IMPORT.value:
            raise PayeeBankError("open_proposal_exists", "this payee already has a proposal awaiting decision")
        # A human correction replaces an open (typically incomplete) legacy-import proposal in
        # the same transaction. Reject the old row BEFORE inserting: one proposed row per payee.
        stamp = row.proposed_at
        open_proposal.status = InstructionStatus.REJECTED.value
        open_proposal.decided_by_party_id = proposer_party_id
        open_proposal.decided_at = stamp
        open_proposal.decision_note = f"replaced by {row.id}"
        db.flush()
        _audit(
            db,
            tenant_id=tenant_id,
            actor=proposer_party_id,
            action=AUDIT_REJECTED,
            row=open_proposal,
            replaced_by=str(row.id),
        )
    db.add(row)
    db.flush()
    _audit(db, tenant_id=tenant_id, actor=proposer_party_id, action=AUDIT_PROPOSED, row=row)
    return row


def approve(
    db: Session,
    *,
    tenant_id: UUID,
    instruction_id: UUID,
    approver_party_id: UUID,
    note: str | None = None,
    now: datetime | None = None,
) -> PayeeBankInstruction:
    row = _locked(db, tenant_id, instruction_id)
    if row.status != InstructionStatus.PROPOSED.value:
        raise PayeeBankError("wrong_state", f"only a proposed instruction can be approved (is {row.status})")
    if row.proposed_by_party_id is not None and row.proposed_by_party_id == approver_party_id:
        raise PayeeBankError("same_person", "the proposer cannot approve their own proposal")
    if resolve_payee(db, tenant_id, row.payee_kind, row.payee_ref) is None:
        raise PayeeBankError("unknown_payee", "payee is not a known supplier or employee in this tenant")
    decision_note = _text(note, "note", limit=500, required=False)
    missing = missing_for_approval(row)
    if missing:
        raise PayeeBankError(
            "incomplete_instruction",
            "cannot approve: invalid or missing " + ", ".join(missing) + "; propose corrected details instead",
        )
    stamp = now or _now()
    previous = db.scalar(
        select(PayeeBankInstruction)
        .where(
            PayeeBankInstruction.tenant_id == tenant_id,
            PayeeBankInstruction.payee_kind == row.payee_kind,
            PayeeBankInstruction.payee_ref == row.payee_ref,
            PayeeBankInstruction.status == InstructionStatus.APPROVED.value,
        )
        .with_for_update()
    )
    if previous is not None:
        previous.status = InstructionStatus.SUPERSEDED.value
        previous.superseded_by_id = row.id
        previous.superseded_at = stamp
        db.flush()
    row.status = InstructionStatus.APPROVED.value
    row.decided_by_party_id = approver_party_id
    row.decided_at = stamp
    row.approved_at = stamp
    row.decision_note = decision_note
    db.flush()
    _audit(
        db,
        tenant_id=tenant_id,
        actor=approver_party_id,
        action=AUDIT_APPROVED,
        row=row,
        superseded=str(previous.id) if previous is not None else None,
        origin=row.origin,
        shared_account_payees=_shared_count(db, row),
    )
    return row


def reject(
    db: Session,
    *,
    tenant_id: UUID,
    instruction_id: UUID,
    decider_party_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> PayeeBankInstruction:
    row = _locked(db, tenant_id, instruction_id)
    if row.status != InstructionStatus.PROPOSED.value:
        raise PayeeBankError("wrong_state", f"only a proposed instruction can be rejected (is {row.status})")
    note = _text(reason, "reason", limit=500)  # validate before any mutation
    row.status = InstructionStatus.REJECTED.value
    row.decided_by_party_id = decider_party_id
    row.decided_at = now or _now()
    row.decision_note = note
    db.flush()
    _audit(db, tenant_id=tenant_id, actor=decider_party_id, action=AUDIT_REJECTED, row=row)
    return row


def revoke(
    db: Session,
    *,
    tenant_id: UUID,
    instruction_id: UUID,
    revoker_party_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> PayeeBankInstruction:
    row = _locked(db, tenant_id, instruction_id)
    if row.status != InstructionStatus.APPROVED.value:
        raise PayeeBankError("wrong_state", f"only an approved instruction can be revoked (is {row.status})")
    note = _text(reason, "reason", limit=500)  # validate before any mutation
    stamp = now or _now()
    row.status = InstructionStatus.REVOKED.value
    row.revoked_by_party_id = revoker_party_id
    row.revoked_at = stamp
    row.decision_note = note
    db.flush()
    _audit(db, tenant_id=tenant_id, actor=revoker_party_id, action=AUDIT_REVOKED, row=row)
    return row


def list_instructions(
    db: Session, *, tenant_id: UUID, status: str | None = None, limit: int = 200
) -> list[PayeeBankView]:
    query = select(PayeeBankInstruction).where(PayeeBankInstruction.tenant_id == tenant_id)
    if status:
        query = query.where(PayeeBankInstruction.status == status)
    rows = db.scalars(query.order_by(PayeeBankInstruction.proposed_at.desc()).limit(min(limit, 500))).all()
    return [view(db, r) for r in rows]


def reveal_account_number(
    db: Session, *, tenant_id: UUID, instruction_id: UUID, actor_party_id: UUID, reason: str
) -> str:
    """Audited unmasked read for the approver's verification against evidence."""

    reason_text = _text(reason, "reason", limit=200)
    row = db.scalar(
        select(PayeeBankInstruction).where(
            PayeeBankInstruction.tenant_id == tenant_id, PayeeBankInstruction.id == instruction_id
        )
    )
    if row is None:
        raise PayeeBankError("not_found", "instruction not found")
    _audit(db, tenant_id=tenant_id, actor=actor_party_id, action=AUDIT_UNMASKED_READ, row=row, reason=reason_text)
    return row.account_number


def destination_for_payment(db: Session, *, tenant_id: UUID, payee_kind: str, payee_ref: str) -> PaymentDestination:
    """The single approved destination for a payee, or a refusal. For B3/B5 file generation.

    Callers are expected to hold the disbursement-file permission and to audit at file level.
    Proposed, rejected, superseded and revoked rows are never returned.
    """

    row = db.scalar(
        select(PayeeBankInstruction).where(
            PayeeBankInstruction.tenant_id == tenant_id,
            PayeeBankInstruction.payee_kind == payee_kind,
            PayeeBankInstruction.payee_ref == payee_ref,
            PayeeBankInstruction.status == InstructionStatus.APPROVED.value,
        )
    )
    if row is None:
        raise PayeeBankError("no_approved_instruction", "payee has no approved bank instruction")
    if missing_for_approval(row):  # approved rows were validated; defence in depth
        raise PayeeBankError("incomplete_instruction", "approved instruction is not usable")
    return PaymentDestination(
        instruction_id=row.id,
        bank_code=row.bank_code or "",
        account_number=row.account_number,
        account_name=row.account_name or "",
        payee_kind=row.payee_kind,
        payee_ref=row.payee_ref,
    )
