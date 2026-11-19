"""Synthetic legacy AR rows and a complete a12 manifest for the AR opening tests (documentation values only)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from dotmac_billing.contracts import (
    PartyDocumentSnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
)
from dotmac_billing.opening_contracts import OpeningCaptureV1, OpeningPartitionV1

from erp_v2.importer.billing_opening import (
    OpeningTranslationContext,
    build_complete_manifest,
    translate_partition,
)

UTC = timezone.utc
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
SNAPSHOT = PartyDocumentSnapshotV1(
    legal_name="Party Ltd", address=PostalAddressSnapshotV1(line_one="1 Example Rd", country_code="NG")
)
CUTOFF = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)


def account_id(key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"test-account:{key}")


def partition(key: str, currency: str = "NGN") -> OpeningPartitionV1:
    return OpeningPartitionV1(source_account_key=key, account_id=account_id(key), currency=currency, precision=2)


def context(key: str, currency: str = "NGN") -> OpeningTranslationContext:
    return OpeningTranslationContext(
        partition=partition(key, currency),
        seller=SNAPSHOT,
        customer_snapshot=lambda _k: SNAPSHOT,
        payment_instructions=PaymentInstructionsSnapshotV1(method_code="bank_transfer"),
    )


def invoice(iid: str, customer: str = "cust-1", total: str = "1000.00", **kw):
    base = {
        "invoice_id": iid,
        "invoice_number": iid.upper(),
        "customer_key": customer,
        "invoice_date": datetime(2025, 3, 5, tzinfo=UTC),
        "due_date": date(2025, 4, 4),
        "currency_code": "NGN",
        "subtotal": Decimal(total),
        "tax_amount": Decimal("0.00"),
        "total_amount": Decimal(total),
        "status": "POSTED",
        "invoice_type": "STANDARD",
        "dotmac_sub_id": None,
        "splynx_id": None,
    }
    base.update(kw)
    return base


def payment(pid: str, amount: str, customer: str = "cust-1", **kw):
    base = {
        "payment_id": pid,
        "customer_key": customer,
        "amount": Decimal(amount),
        "payment_date": datetime(2025, 6, 1, tzinfo=UTC),
        "reference": f"REF-{pid}",
        "currency_code": "NGN",
        "status": "POSTED",
        "dotmac_sub_id": None,
        "splynx_id": None,
    }
    base.update(kw)
    return base


def alloc(pid: str, iid: str, amount: str):
    return {
        "payment_id": pid,
        "invoice_id": iid,
        "amount_allocated": Decimal(amount),
        "allocated_at": datetime(2025, 7, 1, tzinfo=UTC),
    }


def capture() -> OpeningCaptureV1:
    return OpeningCaptureV1(
        source_system="legacy-erp",
        source_repository_sha="a" * 40,
        installation_id="legacy-snapshot-1",
        schema_heads=("legacy-head",),
        snapshot_id="b" * 64,
        captured_at=datetime(2026, 11, 20, tzinfo=UTC),
        economic_cutoff=CUTOFF,
        recorded_cutoff=CUTOFF,
        isolation="repeatable_read",
        population_version="1",
        source_authority_ref="test-source-authority",
        account_mapping_version="erp-billing-opening/2",
        cash_mapping_version="erp-billing-opening/2",
    )


def two_partition_manifest():
    t1 = translate_partition(
        context=context("cust-1"),
        invoices=[
            invoice("inv-1", total="1000.00", dotmac_sub_id="sub-9"),
            invoice("inv-2", total="500.00", splynx_id="spx-1"),
            invoice("inv-3", total="40.00"),
            invoice("inv-closed", total="30.00"),
        ],
        allocations=[
            alloc("pay-1", "inv-1", "400.00"),
            alloc("pay-closed", "inv-closed", "30.00"),
            alloc("pay-2", "inv-3", "10.00"),
        ],
        payments=[payment("pay-1", "450.00"), payment("pay-2", "10.00"), payment("pay-closed", "30.00")],
    )
    t2 = translate_partition(
        context=context("cust-2"),
        invoices=[invoice("inv-5", customer="cust-2", total="75.50")],
        allocations=[],
        payments=[payment("pay-9", "20.00", customer="cust-2")],
    )
    return (t1, t2), build_complete_manifest(tenant_id=TENANT, capture=capture(), translations=(t1, t2))


def assembly(*, extra_invoices=(), extra_payments=(), extra_allocations=()):
    """A census-backed ArAssembly over the two-partition fixture (+ optional extra rows)."""

    from erp_v2.importer.ar_assembly import ArAssembly
    from erp_v2.importer.ar_census import build_census, normalise_rows

    invoices = [
        invoice("inv-1", total="1000.00", dotmac_sub_id="sub-9", posted=True),
        invoice("inv-2", total="500.00", splynx_id="spx-1", posted=True),
        invoice("inv-3", total="40.00", posted=True),
        invoice("inv-closed", total="30.00", posted=True),
        invoice("inv-5", customer="cust-2", total="75.50", posted=True),
        *extra_invoices,
    ]
    payments = [
        payment("pay-1", "450.00", posted=True),
        payment("pay-2", "10.00", posted=True),
        payment("pay-closed", "30.00", posted=True),
        payment("pay-9", "20.00", customer="cust-2", posted=True),
        *extra_payments,
    ]
    allocations = [
        alloc("pay-1", "inv-1", "400.00"),
        alloc("pay-closed", "inv-closed", "30.00"),
        alloc("pay-2", "inv-3", "10.00"),
        *extra_allocations,
    ]
    census = build_census(
        snapshot_sha256="b" * 64,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=payments,
        allocations=allocations,
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    ni, np_, na = normalise_rows(invoices, payments, allocations)
    return ArAssembly(
        tenant_id=TENANT,
        capture=capture(),
        census=census,
        invoices=ni,
        payments=np_,
        allocations=na,
        account_ids={(c, "NGN"): account_id(c) for c in ("cust-1", "cust-2")},
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
