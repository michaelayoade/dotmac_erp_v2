"""Billing opening translation (rev 2): one complete a12 manifest, F1 reversed, whole-partition exclusion."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest
from ar_opening_support import (
    TENANT,
    UTC,
    alloc,
    capture,
    context,
    invoice,
    partition,
    payment,
    two_partition_manifest,
)
from dotmac_billing import OpeningTargetV1, opening_manifest_fingerprint_v2
from dotmac_billing.opening_receivable_validation import (
    opening_account_import_blockers_v2,
    preview_opening_account_import_batch_v2,
)
from dotmac_kernel.cache import TenantScope

from erp_v2.importer.billing_opening import (
    OpeningTranslationError,
    build_complete_manifest,
    build_import_command,
    translate_partition,
)


def _target(p):
    return OpeningTargetV1(
        scope=TenantScope(TENANT),
        account_id=p.account_id,
        currency=p.currency,
        precision=p.precision,
        expected_position_fingerprint="0" * 64,
        posting_watermark="0",
    )


def _cmd(manifest, p):
    return build_import_command(
        manifest=manifest,
        target=_target(p),
        migration_authority_ref="dotmac-debt-register v1",
        occurred_at=datetime(2026, 11, 20, tzinfo=UTC),
        partition=p,
    )


def _reasons(t):
    return {(b.source_key, b.reason) for b in t.blockers}


def test_every_origin_is_translated_with_origin_independent_identity() -> None:
    (t1, _t2), _m = two_partition_manifest()
    by_key = {i.source_invoice_key: i for i in t1.items}
    assert set(by_key) == {"inv-1", "inv-2", "inv-3"}  # Sub-origin, Splynx and native, all admitted
    assert {i.source_identity.kind for i in t1.items} == {"invoice"}
    # origin lives in the evidence digest, not the identity: same key, different origin, different digest
    native = translate_partition(context=context("c"), invoices=[invoice("x")], allocations=[], payments=[])
    sub = translate_partition(
        context=context("c"), invoices=[invoice("x", dotmac_sub_id="s-1")], allocations=[], payments=[]
    )
    assert native.items[0].source_identity == sub.items[0].source_identity
    assert native.items[0].source_evidence_digest != sub.items[0].source_evidence_digest
    assert t1.origin_residual_minor == {"native": 3000, "dotmac_sub": 60000, "splynx": 50000}


def test_residual_is_total_minus_allocations_and_discharges_are_exact() -> None:
    (t1, _), _m = two_partition_manifest()
    inv1 = next(i for i in t1.items if i.source_invoice_key == "inv-1")
    assert inv1.residual_minor == 60000
    assert [d.amount_minor for d in inv1.historical_discharges] == [40000]
    assert inv1.historical_discharges[0].source_key == "pay-1"


def test_every_discharging_payment_is_a_cash_lot_and_unallocated_cash_is_the_residual() -> None:
    (t1, t2), _m = two_partition_manifest()
    lots = {lot.source_identity.key: lot for lot in t1.cash_lots}
    assert set(lots) == {"pay-1", "pay-2"}  # pay-closed only touched a closed invoice: history, no lot
    assert lots["pay-1"].gross_minor == 45000 and lots["pay-1"].residual_minor == 5000
    assert lots["pay-2"].residual_minor == 0  # fully allocated, still a lot (its discharge needs it)
    assert t1.ignored_payments == ("pay-closed",)
    assert [lot.residual_minor for lot in t2.cash_lots] == [2000]  # unallocated cash with no discharge


def test_closed_invoice_is_history_not_an_item() -> None:
    (t1, _), _m = two_partition_manifest()
    assert t1.closed_invoices == ("inv-closed",)
    assert "inv-closed" not in {i.source_invoice_key for i in t1.items}


def test_unapplied_credit_note_excludes_the_whole_partition() -> None:
    t = translate_partition(
        context=context("c"),
        invoices=[invoice("inv-1"), invoice("cn-1", total="-25.00", invoice_type="CREDIT_NOTE")],
        allocations=[],
        payments=[payment("p-1", "10.00")],
    )
    assert not t.carryable and t.items == () and t.cash_lots == ()  # nothing partial leaks out
    assert _reasons(t) == {("cn-1", "credit_transfer_unsupported")}


def test_over_allocated_invoice_is_a_named_blocker() -> None:
    t = translate_partition(
        context=context("c"),
        invoices=[invoice("inv-1", total="100.00")],
        allocations=[alloc("p-1", "inv-1", "70.00"), alloc("p-2", "inv-1", "70.00")],
        payments=[payment("p-1", "70.00"), payment("p-2", "70.00")],
    )
    assert _reasons(t) == {("inv-1", "invoice_residual_nonpositive")}


def test_allocation_to_an_invoice_outside_the_population_blocks() -> None:
    t = translate_partition(
        context=context("c"),
        invoices=[invoice("inv-1")],
        allocations=[alloc("p-1", "ghost", "10.00")],
        payments=[payment("p-1", "10.00")],
    )
    assert _reasons(t) == {("p-1", "payment_allocation_outside_population")}


def test_allocation_by_a_payment_of_another_partition_blocks_the_invoice() -> None:
    t = translate_partition(
        context=context("c"),
        invoices=[invoice("inv-1")],
        allocations=[alloc("other", "inv-1", "10.00")],
        payments=[],
    )
    assert _reasons(t) == {("inv-1", "payment_allocation_outside_population")}


def test_inexact_amount_is_a_blocker_never_rounded() -> None:
    t = translate_partition(
        context=context("c"),
        invoices=[invoice("inv-1", total="10.001")],
        allocations=[],
        payments=[payment("p-1", "5.005")],
    )
    assert _reasons(t) == {("inv-1", "amount_not_exact"), ("p-1", "amount_not_exact")}


def test_overallocated_payment_is_a_blocker() -> None:
    t = translate_partition(
        context=context("c"),
        invoices=[invoice("inv-1", total="100.00")],
        allocations=[alloc("p-1", "inv-1", "60.00")],
        payments=[payment("p-1", "50.00")],
    )
    assert ("p-1", "payment_overallocated") in _reasons(t)


def test_missing_provenance_column_is_refused_not_treated_as_native() -> None:
    row = invoice("inv-1")
    del row["splynx_id"]
    with pytest.raises(OpeningTranslationError, match="lacks splynx_id"):
        translate_partition(context=context("c"), invoices=[row], allocations=[], payments=[])
    pay = payment("p-1", "1.00")
    del pay["dotmac_sub_id"]
    with pytest.raises(OpeningTranslationError, match="lacks dotmac_sub_id"):
        translate_partition(context=context("c"), invoices=[invoice("inv-1")], allocations=[], payments=[pay])


def test_duplicate_ids_refuse() -> None:
    with pytest.raises(OpeningTranslationError, match="duplicate invoice"):
        translate_partition(
            context=context("c"), invoices=[invoice("inv-1"), invoice("inv-1")], allocations=[], payments=[]
        )


def test_collection_evidence_stays_unknown_unverified() -> None:
    (t1, _), _m = two_partition_manifest()
    assert {i.collection.status for i in t1.items} == {"unknown_unverified"}


def test_stored_balance_difference_is_an_exact_variance_and_the_ledger_closes() -> None:
    t = translate_partition(
        context=context("c"),
        invoices=[invoice("inv-1", total="100.00", balance_due=Decimal("90.00"))],
        allocations=[],
        payments=[],
    )
    (v,) = t.variances
    assert (v.basis, v.reason_code, v.amount_minor) == ("stored_balance", "stored_balance_difference", -1000)
    manifest = build_complete_manifest(tenant_id=TENANT, capture=capture(), translations=(t,))
    assert opening_account_import_blockers_v2(_cmd(manifest, t.partition)) == ()


def test_one_manifest_with_partition_selection_has_no_blockers_for_any_partition() -> None:
    (t1, t2), manifest = two_partition_manifest()
    assert len(manifest.controls) == 2 and len(manifest.measures) == 2
    commands = [_cmd(manifest, t.partition) for t in (t1, t2)]
    assert all(opening_account_import_blockers_v2(c) == () for c in commands)
    batch = preview_opening_account_import_batch_v2(commands)
    assert batch.manifest_blockers == () and all(i.partition_blockers == () for i in batch.items)
    # one fingerprint across every command
    assert len({opening_manifest_fingerprint_v2(c.manifest) for c in commands}) == 1


def test_measures_and_controls_are_consistent_with_the_items_and_lots() -> None:
    (t1, _), manifest = two_partition_manifest()
    control = next(c for c in manifest.controls if c.partition == t1.partition)
    measures = next(m for m in manifest.measures if m.partition == t1.partition)
    assert control.open_ar_minor == control.collectible_receivable_minor == 113000
    assert control.available_cash_credit_minor == 5000 and control.unapplied_customer_credit_minor == 0
    assert measures.document_residual_minor == measures.stored_balance_minor == 113000
    assert measures.rebuild_minor == 113000 - 5000
    assert measures.population_rows == measures.transferred_rows == 5 and measures.non_carryable_rows == 0


def test_manifest_refuses_a_blocked_partition_and_a_duplicate() -> None:
    blocked = translate_partition(
        context=context("c"),
        invoices=[invoice("cn", total="-1.00", invoice_type="CREDIT_NOTE")],
        allocations=[],
        payments=[],
    )
    with pytest.raises(OpeningTranslationError, match="only carryable"):
        build_complete_manifest(tenant_id=TENANT, capture=capture(), translations=(blocked,))
    ok = translate_partition(context=context("c"), invoices=[invoice("a")], allocations=[], payments=[])
    with pytest.raises(OpeningTranslationError, match="duplicate partition"):
        build_complete_manifest(tenant_id=TENANT, capture=capture(), translations=(ok, ok))
    with pytest.raises(OpeningTranslationError, match="no translated partitions"):
        build_complete_manifest(tenant_id=TENANT, capture=capture(), translations=())


def test_command_needs_an_explicit_partition_on_a_complete_manifest_and_refuses_a_stranger() -> None:
    (t1, _t2), manifest = two_partition_manifest()
    with pytest.raises(OpeningTranslationError, match="needs the partition"):
        build_import_command(
            manifest=manifest,
            target=_target(t1.partition),
            migration_authority_ref="x",
            occurred_at=datetime(2026, 11, 20, tzinfo=UTC),
        )
    with pytest.raises(OpeningTranslationError, match="not in the manifest"):
        _cmd(manifest, partition("stranger"))
    with pytest.raises(OpeningTranslationError, match="disagrees"):
        build_import_command(
            manifest=manifest,
            target=replace(_target(t1.partition), currency="USD"),
            migration_authority_ref="x",
            occurred_at=datetime(2026, 11, 20, tzinfo=UTC),
            partition=t1.partition,
        )


def test_customer_snapshot_missing_is_a_blocker() -> None:
    ctx = replace(context("c"), customer_snapshot=lambda _k: None)
    t = translate_partition(context=ctx, invoices=[invoice("inv-1")], allocations=[], payments=[])
    assert _reasons(t) == {("inv-1", "customer_snapshot_missing")}
