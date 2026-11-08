"""Billing opening translation: evidence in, exact residuals out."""

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
    build_manifest,
    translate_partition,
)

UTC = timezone.utc
PARTITION = OpeningPartitionV1(source_account_key="cust-1", account_id=uuid.uuid4(), currency="NGN", precision=2)
SNAPSHOT = PartyDocumentSnapshotV1(
    legal_name="Party Ltd",
    address=PostalAddressSnapshotV1(line_one="1 Harbour Rd", country_code="NG"),
)
CTX = OpeningTranslationContext(
    partition=PARTITION,
    seller=SNAPSHOT,
    customer_snapshot=lambda _key: SNAPSHOT,
    payment_instructions=PaymentInstructionsSnapshotV1(method_code="bank_transfer"),
)


def _invoice(**overrides):
    base = {
        "invoice_id": "inv-1",
        "invoice_number": "INV-1",
        "customer_key": "cust-1",
        "invoice_date": datetime(2026, 1, 5, tzinfo=UTC),
        "due_date": date(2026, 2, 4),
        "currency_code": "NGN",
        "subtotal": Decimal("930.23"),
        "tax_amount": Decimal("69.77"),
        "total_amount": Decimal("1000.00"),
        "status": "posted",
    }
    base.update(overrides)
    return base


def test_open_invoice_residual_after_partial_payment() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[_invoice()],
        allocations=[
            {
                "invoice_id": "inv-1",
                "payment_id": "pay-1",
                "amount_allocated": Decimal("400.00"),
                "allocated_at": datetime(2026, 1, 20, tzinfo=UTC),
            }
        ],
        payments=[
            {
                "payment_id": "pay-1",
                "amount": Decimal("400.00"),
                "payment_date": datetime(2026, 1, 20, tzinfo=UTC),
                "reference": "RCPT-1",
                "status": "confirmed",
            }
        ],
    )
    assert not result.blockers
    (item,) = result.items
    assert item.residual_minor == 60000
    assert item.original is not None and item.original.document_number == "INV-1"
    assert item.collection.status == "unknown_unverified"
    assert item.collection.automated_collection_allowed is False
    assert result.open_ar_minor == 60000
    assert result.cash_lots == ()


def test_void_overpaid_inexact_and_unallocated_cash() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[
            _invoice(invoice_id="inv-void", status="voided"),
            _invoice(invoice_id="inv-over", invoice_number="INV-2"),
            _invoice(invoice_id="inv-bad", invoice_number="INV-3", total_amount=Decimal("10.001")),
        ],
        allocations=[
            {
                "invoice_id": "inv-over",
                "payment_id": "pay-9",
                "amount_allocated": Decimal("1200.00"),
                "allocated_at": datetime(2026, 1, 20, tzinfo=UTC),
            }
        ],
        payments=[
            {
                "payment_id": "pay-9",
                "amount": Decimal("1500.00"),
                "payment_date": datetime(2026, 1, 20, tzinfo=UTC),
                "reference": "RCPT-9",
                "status": "confirmed",
            },
            {
                "payment_id": "pay-free",
                "amount": Decimal("50.00"),
                "payment_date": datetime(2026, 1, 21, tzinfo=UTC),
                "reference": "RCPT-10",
                "status": "confirmed",
            },
        ],
    )
    assert result.items == ()
    reasons = {b.source_key: b.reason for b in result.blockers}
    assert "overpaid" in reasons["inv-over"]
    assert "precision" in reasons["inv-bad"]
    dispositions = {d.identity.key: d.disposition for d in result.dispositions}
    assert dispositions["inv-void"] == "excluded"
    lots = {lot.lot_id: lot.residual_minor for lot in result.cash_lots}
    assert lots == {"legacy-payment:pay-9": 30000, "legacy-payment:pay-free": 5000}


def test_manifest_controls_and_counts() -> None:
    result = translate_partition(context=CTX, invoices=[_invoice()], allocations=[], payments=[])
    tenant_id = uuid.uuid4()
    capture = OpeningCaptureV1(
        source_system="legacy-erp",
        source_repository_sha="74fa788a",
        installation_id="prod",
        schema_heads=("legacy-head",),
        snapshot_id="snap-1",
        captured_at=datetime(2026, 11, 8, tzinfo=UTC),
        economic_cutoff=datetime(2026, 11, 8, tzinfo=UTC),
        recorded_cutoff=datetime(2026, 11, 8, tzinfo=UTC),
        isolation="repeatable-read-dump",
        population_version="1",
        source_authority_ref="ops:2026-11-08",
        account_mapping_version="erp-billing-opening/1",
        cash_mapping_version="erp-billing-opening/1",
    )
    manifest = build_manifest(tenant_id=tenant_id, capture=capture, translation=result)
    assert manifest.counts.eligible == 1
    assert manifest.controls[0].open_ar_minor == 100000
    assert manifest.controls[0].collectible_receivable_minor == 0
    assert manifest.receivables[0].residual_minor == 100000


def _capture() -> OpeningCaptureV1:
    return OpeningCaptureV1(
        source_system="legacy-erp",
        source_repository_sha="74fa788a",
        installation_id="prod",
        schema_heads=("legacy-head",),
        snapshot_id="snap-1",
        captured_at=datetime(2026, 11, 8, tzinfo=UTC),
        economic_cutoff=datetime(2026, 11, 8, tzinfo=UTC),
        recorded_cutoff=datetime(2026, 11, 8, tzinfo=UTC),
        isolation="repeatable-read-dump",
        population_version="1",
        source_authority_ref="ops:2026-11-08",
        account_mapping_version="erp-billing-opening/1",
        cash_mapping_version="erp-billing-opening/1",
    )


def test_preflight_round_trip_without_verifier_is_honest() -> None:
    import pytest
    from dotmac_billing import OpeningTargetV1, opening_manifest_fingerprint_v2
    from dotmac_kernel.cache import TenantScope

    from erp_v2.importer.billing_opening import (
        OpeningTranslationError,
        build_import_command,
        preflight_opening_import,
    )

    result = translate_partition(context=CTX, invoices=[_invoice()], allocations=[], payments=[])
    tenant_id = uuid.uuid4()
    manifest = build_manifest(tenant_id=tenant_id, capture=_capture(), translation=result)
    target = OpeningTargetV1(
        scope=TenantScope(tenant_id),
        account_id=PARTITION.account_id,
        currency="NGN",
        precision=2,
        expected_position_fingerprint="dest-observed-fingerprint",
        posting_watermark="dest-watermark",
    )
    preflight = preflight_opening_import(
        translation=result,
        manifest=manifest,
        target=target,
        migration_authority_ref="approval:2026-11-08",
        occurred_at=datetime(2026, 11, 8, tzinfo=UTC),
    )
    # Fingerprint agrees with the module's own computation, residuals
    # round-trip, and with no verifier configured the module preview
    # refuses admissibility - never importable in this state.
    assert preflight.manifest_fingerprint == opening_manifest_fingerprint_v2(manifest)
    assert preflight.receivable_residual_minor == 100000
    assert preflight.importable is False
    assert "verifier_unavailable" in preflight.preview_blocker_codes

    bad_target = OpeningTargetV1(
        scope=TenantScope(tenant_id),
        account_id=PARTITION.account_id,
        currency="USD",
        precision=2,
        expected_position_fingerprint="x",
        posting_watermark="y",
    )
    with pytest.raises(OpeningTranslationError, match="disagrees"):
        build_import_command(
            manifest=manifest,
            target=bad_target,
            migration_authority_ref="approval:2026-11-08",
            occurred_at=datetime(2026, 11, 8, tzinfo=UTC),
        )
