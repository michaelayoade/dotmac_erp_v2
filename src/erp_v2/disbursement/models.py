"""Disbursement file receipts (e2_0018): sha256 + row count + which payee instruction, never bytes.

Insert-only for the runtime role. One row per document in a file (a batch file writes one row per
payment, all sharing the file's sha256 and row count). The receipt lets recording re-resolve the
payee destination and refuse when it changed after the file was generated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

KIND_SUPPLIER_PAYMENT: Final = "supplier_payment"
KIND_PAYROLL_PAYOUT: Final = "payroll_payout"
DISBURSEMENT_TABLES: Final = ("erp_disbursement_file_receipts",)


class DisbursementFileReceipt(Base):
    __tablename__ = "erp_disbursement_file_receipts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_erp_disbursement_file_receipts_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "document_kind", "document_id", name="uq_erp_disbursement_file_receipts_document"
        ),
        CheckConstraint(
            "document_kind IN ('supplier_payment', 'payroll_payout')", name="ck_erp_disbursement_file_receipts_kind"
        ),
        CheckConstraint("row_count > 0", name="ck_erp_disbursement_file_receipts_rows"),
        CheckConstraint("char_length(file_sha256) = 64", name="ck_erp_disbursement_file_receipts_sha"),
        Index("ix_erp_disbursement_file_receipts_document", "tenant_id", "document_kind", "document_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    document_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    payee_instruction_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    file_format: Mapped[str] = mapped_column(String(32), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_by_party_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
