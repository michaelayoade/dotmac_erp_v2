"""One bank-evidence-reference rule shared by B3 supplier payments and B5 payroll payouts.

A bank reference is used once per (tenant, bank account) across BOTH modules (a B5 bank
return reference counts too). Each module has its own
table (and its own unique constraint), so no single constraint can span them; the cross-module rule is
enforced here, serialised by a transaction-scoped advisory lock.

Lock key (stable across processes and releases; do not change without a migration plan):
    key = signed big-endian int64 of the first 8 bytes of
          sha256("erp_v2.bank_reference" 0x1f tenant_id 0x1f bank_account_id 0x1f normalized_reference)
with tenant_id / bank_account_id as canonical lower-case UUID strings. The lock is taken with
pg_advisory_xact_lock(key) so it is released on commit or rollback and is never held across requests.
A hash collision can only make two unrelated references wait for each other; it can never skip a check,
because the existence checks below always run after the lock.
"""

from __future__ import annotations

import hashlib
import struct
from uuid import UUID

from dotmac_payables.models import SupplierPayment
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from erp_v2.payroll_payouts.models import PayrollPayoutBatch, PayrollPayoutLine

MIN_LENGTH = 3
MAX_LENGTH = 255
_NAMESPACE = "erp_v2.bank_reference"


class BankReferenceInvalid(ValueError):
    """`reason` is "control" (control characters) or "length"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BankReferenceReused(Exception):
    pass


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 32 or code == 127 or 0x80 <= code <= 0x9F  # C0, DEL and C1 controls


def normalize(value: str | None) -> str:
    """Trim, collapse inner whitespace, upper-case. Refuses control characters and bad lengths.

    Control characters are checked on the whole input (C1 NEL, U+0085, would otherwise be swallowed as
    whitespace). Leading/trailing C0 whitespace such as a newline is still trimmed, as before.
    """
    raw = value or ""
    if any(0x80 <= ord(c) <= 0x9F for c in raw) or any(_is_control(c) for c in raw.strip()):
        raise BankReferenceInvalid("control")
    text_value = " ".join(raw.split()).upper()
    if len(text_value) < MIN_LENGTH or len(text_value) > MAX_LENGTH:
        raise BankReferenceInvalid("length")
    return text_value


def lock_key(tenant_id: UUID, bank_account_id: UUID, reference: str) -> int:
    material = "\x1f".join((_NAMESPACE, str(tenant_id).lower(), str(bank_account_id).lower(), reference))
    return struct.unpack(">q", hashlib.sha256(material.encode("utf-8")).digest()[:8])[0]


def claim(
    db: Session,
    *,
    tenant_id: UUID,
    bank_account_id: UUID,
    reference: str,
    exclude_payroll_batch_id: UUID | None = None,
    exclude_supplier_payment_id: UUID | None = None,
    check_supplier_payments: bool = True,
) -> None:
    """Lock the (tenant, account, reference) key for this transaction, then refuse if it is already used.

    `reference` must already be normalised. The caller commits or rolls back; the lock goes with it.
    Raises BankReferenceReused.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key(tenant_id, bank_account_id, reference)})
    payroll = select(PayrollPayoutBatch.id).where(
        PayrollPayoutBatch.tenant_id == tenant_id,
        PayrollPayoutBatch.bank_account_id == bank_account_id,
        PayrollPayoutBatch.bank_reference == reference,
    )
    if exclude_payroll_batch_id is not None:
        payroll = payroll.where(PayrollPayoutBatch.id != exclude_payroll_batch_id)
    if db.scalar(payroll.limit(1)) is not None:
        raise BankReferenceReused
    # A bank RETURN reference (B5, payroll a5) is also a bank evidence reference on this account.
    returned = select(PayrollPayoutLine.id).where(
        PayrollPayoutLine.tenant_id == tenant_id,
        PayrollPayoutLine.return_bank_account_id == bank_account_id,
        PayrollPayoutLine.return_reference == reference,
    )
    if db.scalar(returned.limit(1)) is not None:
        raise BankReferenceReused
    if check_supplier_payments:
        supplier = select(SupplierPayment.id).where(
            SupplierPayment.tenant_id == tenant_id,
            SupplierPayment.payer_instrument_ref == str(bank_account_id),
            func.upper(SupplierPayment.bank_reference) == reference,
        )
        if exclude_supplier_payment_id is not None:
            supplier = supplier.where(SupplierPayment.id != exclude_supplier_payment_id)
        if db.scalar(supplier.limit(1)) is not None:
            raise BankReferenceReused
