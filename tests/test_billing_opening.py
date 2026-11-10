"""Billing opening translation: evidence in, exact residuals out."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from dotmac_billing.contracts import (
    PartyDocumentSnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
)
from dotmac_billing.opening_contracts import OpeningCaptureV1, OpeningPartitionV1

from erp_v2.importer.billing_opening import (
    OpeningTranslationContext,
    OpeningTranslationError,
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
        "dotmac_sub_id": None,
        "splynx_id": None,
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
                "dotmac_sub_id": None,
                "splynx_id": None,
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
                "dotmac_sub_id": None,
                "splynx_id": None,
            },
            {
                "payment_id": "pay-free",
                "amount": Decimal("50.00"),
                "payment_date": datetime(2026, 1, 21, tzinfo=UTC),
                "reference": "RCPT-10",
                "status": "confirmed",
                "dotmac_sub_id": None,
                "splynx_id": None,
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


# --- F1: Sub-origin receivables are never ERP v2's (census + exclusion) ---

AT = datetime(2026, 1, 20, tzinfo=UTC)


def _alloc(invoice_id: str, payment_id: str, amount: str) -> dict:
    return {
        "invoice_id": invoice_id,
        "payment_id": payment_id,
        "amount_allocated": Decimal(amount),
        "allocated_at": AT,
    }


def _payment(payment_id: str, amount: str, **overrides) -> dict:
    row = {
        "payment_id": payment_id,
        "amount": Decimal(amount),
        "payment_date": AT,
        "reference": f"RCPT-{payment_id}",
        "status": "confirmed",
        "dotmac_sub_id": None,
        "splynx_id": None,
    }
    row.update(overrides)
    return row


def _reasons(result) -> dict[str, str]:
    return {b.source_key: b.reason for b in result.blockers}


def test_sub_origin_invoices_excluded_with_allocations_payments_and_credit_notes() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[
            _invoice(invoice_id="native-1", invoice_number="N-1"),
            _invoice(invoice_id="sub-1", invoice_number="S-1", dotmac_sub_id="sub-ext-1"),
            _invoice(invoice_id="sub-cn", invoice_number="S-CN", dotmac_sub_id="sub-ext-2", invoice_type="CREDIT_NOTE"),
        ],
        allocations=[_alloc("sub-1", "pay-sub", "300.00"), _alloc("native-1", "pay-nat", "100.00")],
        payments=[_payment("pay-sub", "300.00", dotmac_sub_id="sx"), _payment("pay-nat", "100.00")],
    )
    assert not result.blockers
    assert [i.source_invoice_key for i in result.items] == ["native-1"]
    assert result.items[0].residual_minor == 90000
    assert result.cash_lots == ()
    ex = result.exclusions
    assert ex.invoices == ("sub-1",)
    assert ex.credit_notes == ("sub-cn",)
    assert ex.allocations == ("pay-sub:sub-1",)
    assert ex.payments == ("pay-sub",)
    assert ex.mixed_payments == ()
    assert ex.totals_minor["payments"] == 30000
    excluded = {d.identity.key: d for d in result.dispositions if d.disposition == "excluded"}
    assert set(excluded) == {"sub-1", "sub-cn", "pay-sub"}
    assert all(d.reason == "sub_origin" for d in excluded.values())
    assert result.census["balanced"] is True
    assert result.census["invoices"] == {
        "source": 3,
        "translated": 1,
        "excluded_void": 0,
        "excluded_sub_origin": 2,
        "blocked": 0,
    }
    assert result.census["payments"]["excluded_sub_origin"] == 1


def test_erp_payment_on_sub_invoice_blocks_with_evidence_and_no_split() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[
            _invoice(invoice_id="native-1", invoice_number="N-1"),
            _invoice(invoice_id="sub-1", invoice_number="S-1", dotmac_sub_id="x"),
        ],
        allocations=[_alloc("native-1", "pay-mix", "100.00"), _alloc("sub-1", "pay-mix", "250.00")],
        payments=[_payment("pay-mix", "400.00")],
    )
    reasons = _reasons(result)
    assert "exceptions review" in reasons["pay-mix"] and "unapplied 5000" in reasons["pay-mix"]
    assert reasons["native-1"] == "discharged by payment under review"
    assert result.cash_lots == () and result.items == ()
    (split,) = result.exclusions.mixed_payments
    assert (split.gross_minor, split.sub_share_minor, split.native_share_minor, split.remainder_minor) == (
        40000,
        25000,
        10000,
        5000,
    )
    assert result.exclusions.payments == ()  # not "excluded whole": ERP cash is never dropped silently
    assert result.census["payments"]["blocked"] == 1


def test_erp_payment_wholly_on_sub_invoices_with_remainder_blocks_and_reports_amount() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[_invoice(invoice_id="sub-1", invoice_number="S-1", dotmac_sub_id="x")],
        allocations=[_alloc("sub-1", "pay-1", "100.00")],
        payments=[_payment("pay-1", "130.00")],
    )
    assert "unapplied 3000" in _reasons(result)["pay-1"]
    assert result.exclusions.payments == ()
    assert result.cash_lots == ()


def test_allocation_to_absent_void_or_blocked_invoice_blocks_payment() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[
            _invoice(invoice_id="void-1", invoice_number="V", status="voided"),
            _invoice(invoice_id="bad-1", invoice_number="B", total_amount=Decimal("10.001")),
            _invoice(invoice_id="ok-1", invoice_number="O"),
        ],
        allocations=[
            _alloc("ghost", "pay-a", "10.00"),
            _alloc("void-1", "pay-v", "10.00"),
            _alloc("bad-1", "pay-b", "10.00"),
            _alloc("ok-1", "pay-ok", "10.00"),
        ],
        payments=[
            _payment("pay-a", "10.00"),
            _payment("pay-v", "10.00"),
            _payment("pay-b", "10.00"),
            _payment("pay-ok", "10.00"),
        ],
    )
    reasons = _reasons(result)
    assert reasons["pay-a"] == "allocation targets invoice absent from export"
    assert reasons["pay-v"] == "allocation targets void or blocked invoice"
    assert reasons["pay-b"] == "allocation targets void or blocked invoice"
    assert "pay-ok" not in reasons
    assert result.census["payments"]["blocked"] == 3
    assert result.census["payments"]["fully_allocated"] == 1


def test_row_missing_field_is_refused() -> None:
    for field_name in ("dotmac_sub_id", "splynx_id"):
        bad_invoice = _invoice()
        del bad_invoice[field_name]
        with pytest.raises(OpeningTranslationError, match=field_name):
            translate_partition(context=CTX, invoices=[bad_invoice], allocations=[], payments=[])
        bad_payment = _payment("p1", "5.00")
        del bad_payment[field_name]
        with pytest.raises(OpeningTranslationError, match=field_name):
            translate_partition(context=CTX, invoices=[_invoice()], allocations=[], payments=[bad_payment])


def test_duplicate_ids_refuse_so_census_cannot_mislead() -> None:
    with pytest.raises(OpeningTranslationError, match="duplicate invoice"):
        translate_partition(context=CTX, invoices=[_invoice(), _invoice()], allocations=[], payments=[])


def test_native_rows_unchanged_and_blank_string_is_sub_origin() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[_invoice(), _invoice(invoice_id="blank", invoice_number="B", dotmac_sub_id="")],
        allocations=[_alloc("inv-1", "pay-1", "400.00")],
        payments=[_payment("pay-1", "400.00"), _payment("pay-free", "50.00")],
    )
    assert not result.blockers
    assert [i.source_invoice_key for i in result.items] == ["inv-1"]
    assert result.open_ar_minor == 60000
    assert [lot.source_identity.key for lot in result.cash_lots] == ["pay-free"]
    assert result.exclusions.invoices == ("blank",)  # legacy IS NOT NULL: blank counts


def test_sub_tagged_payment_unallocated_excluded_and_contradiction_blocked() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[_invoice()],
        allocations=[_alloc("inv-1", "pay-c", "10.00")],
        payments=[_payment("pay-s", "20.00", dotmac_sub_id="s"), _payment("pay-c", "10.00", dotmac_sub_id="s")],
    )
    assert result.exclusions.payments == ("pay-s",)
    reasons = _reasons(result)
    assert set(reasons) == {"pay-c", "inv-1"}
    assert reasons["inv-1"] == "discharged by payment under review"


def test_negative_cash_residual_is_a_blocker() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[_invoice(total_amount=Decimal("1000.00"))],
        allocations=[_alloc("inv-1", "pay-1", "500.00")],
        payments=[_payment("pay-1", "400.00")],
    )
    assert "negative cash residual" in _reasons(result)["pay-1"]
    assert result.cash_lots == ()


def test_splynx_era_rows_are_blocked_and_reported() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[_invoice(invoice_id="spl", invoice_number="SP", splynx_id="123"), _invoice()],
        allocations=[],
        payments=[_payment("pay-spl", "5.00", splynx_id="9"), _payment("pay-ok", "5.00")],
    )
    reasons = _reasons(result)
    assert reasons["spl"] == "splynx_era_pending_ruling"
    assert reasons["pay-spl"] == "splynx_era_pending_ruling"
    assert [i.source_invoice_key for i in result.items] == ["inv-1"]
    assert result.census["splynx_era"]["invoices"] == ["spl"]
    assert result.census["splynx_era"]["payments"] == ["pay-spl"]


def test_sub_tagged_customers_on_native_invoices_are_reported() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[_invoice(customer_dotmac_sub_id="c-sub")],
        allocations=[],
        payments=[],
    )
    assert result.census["sub_tagged_customers_on_native_invoices"] == ["cust-1"]
    assert len(result.items) == 1  # reporting only, no exclusion


def test_wholly_sub_origin_partition_translates_to_nothing_with_census() -> None:
    result = translate_partition(
        context=CTX,
        invoices=[_invoice(dotmac_sub_id="x")],
        allocations=[],
        payments=[],
    )
    assert result.items == () and not result.blockers
    assert result.census["invoices"]["excluded_sub_origin"] == 1


def test_exclusions_are_deterministic_regardless_of_input_order() -> None:
    invoices = [_invoice(invoice_id=f"sub-{n}", invoice_number=f"S{n}", dotmac_sub_id="x") for n in (3, 1, 2)]
    allocs = [_alloc(f"sub-{n}", f"pay-{n}", "1.00") for n in (3, 1, 2)]
    pays = [_payment(f"pay-{n}", "1.00", dotmac_sub_id="x") for n in (3, 1, 2)]
    a = translate_partition(context=CTX, invoices=invoices, allocations=allocs, payments=pays)
    b = translate_partition(context=CTX, invoices=invoices[::-1], allocations=allocs[::-1], payments=pays[::-1])
    assert a.exclusions == b.exclusions
    assert a.exclusions.as_dict() == b.exclusions.as_dict()
    assert a.census == b.census
    assert a.exclusions.invoices == ("sub-1", "sub-2", "sub-3")


def test_census_no_receivable_item_for_any_row_with_dotmac_sub_id() -> None:
    values = ["sub-ext", "0", " ", "", "NULL"]
    invoices = [_invoice(invoice_id=f"s{n}", invoice_number=f"S{n}", dotmac_sub_id=v) for n, v in enumerate(values)] + [
        _invoice(invoice_id=f"n{n}", invoice_number=f"N{n}") for n in range(3)
    ]
    result = translate_partition(context=CTX, invoices=invoices, allocations=[], payments=[])
    produced = {i.source_invoice_key for i in result.items}
    sub_keys = {str(r["invoice_id"]) for r in invoices if r["dotmac_sub_id"] is not None}
    assert sub_keys and not (produced & sub_keys)
    assert produced == {"n0", "n1", "n2"}
    assert set(result.exclusions.invoices) == sub_keys
    excluded = {d.identity.key for d in result.dispositions if d.disposition == "excluded"}
    assert excluded == sub_keys
