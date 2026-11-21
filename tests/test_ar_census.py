"""AR census: exactly-once buckets, whole-partition exclusion, sha binding."""

from __future__ import annotations

from datetime import datetime

import pytest
from ar_opening_support import CUTOFF, SNAPSHOT, UTC, alloc, invoice, payment

from erp_v2.importer.ar_census import (
    CARRY_ITEM,
    CARRY_LOT,
    CLOSED,
    EXCLUDED_PARTITION,
    NOT_RECEIVABLE,
    UNAPPLIED_CREDIT_NOTE,
    CensusError,
    build_census,
    prove_partition,
)
from erp_v2.importer.billing_opening import OpeningTranslationError

SHA = "c" * 64


def _inv(iid, customer="cust-1", total="100.00", posted=True, status="POSTED", **kw):
    return invoice(iid, customer=customer, total=total, posted=posted, status=status, **kw)


def _pay(pid, amount, customer="cust-1", posted=True, **kw):
    return payment(pid, amount, customer=customer, posted=posted, **kw)


def _census(invoices, payments=(), allocations=(), snapshot=lambda _k: SNAPSHOT, sha=SHA):
    return build_census(
        snapshot_sha256=sha,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=payments,
        allocations=allocations,
        customer_snapshot=snapshot,
        seller=SNAPSHOT,
    )


def _bucket(census, kind, key):
    return next(r for r in census.rows if r.kind == kind and r.key == key)


def test_every_row_lands_in_exactly_one_bucket_with_all_buckets_exercised() -> None:
    invoices = [
        _inv("open-1", total="100.00"),
        _inv("paid-1", total="30.00"),
        _inv("draft-1", posted=False, status="DRAFT"),
        _inv("sub-1", posted=False, status="SUBMITTED"),
        _inv("void-1", posted=False, status="VOID"),
        _inv("late-1", invoice_date=datetime(2026, 2, 1, tzinfo=UTC)),
        _inv("bad-1", customer="cust-2", total="50.005"),
        _inv("cn-1", customer="cust-2", total="-20.00", invoice_type="CREDIT_NOTE"),
    ]
    payments = [_pay("p-lot", "100.00"), _pay("p-hist", "30.00"), _pay("p-draft", "5.00", posted=False)]
    allocations = [alloc("p-lot", "open-1", "60.00"), alloc("p-hist", "paid-1", "30.00")]
    census = _census(invoices, payments, allocations)
    counts = census.counts()
    assert sum(counts.values()) == len(invoices) + len(payments)  # exactly once
    assert _bucket(census, "invoice", "open-1").bucket == CARRY_ITEM
    assert _bucket(census, "payment", "p-lot").bucket == CARRY_LOT
    assert _bucket(census, "invoice", "paid-1").bucket == CLOSED
    assert _bucket(census, "payment", "p-hist").bucket == CLOSED  # fully allocated to history
    assert _bucket(census, "invoice", "draft-1").bucket == NOT_RECEIVABLE
    assert _bucket(census, "invoice", "sub-1").bucket == NOT_RECEIVABLE
    assert _bucket(census, "invoice", "late-1").bucket == NOT_RECEIVABLE  # issued after the cutoff
    assert _bucket(census, "invoice", "void-1").bucket == CLOSED
    assert _bucket(census, "payment", "p-draft").bucket == NOT_RECEIVABLE  # an unposted payment is a work item
    # cust-2: bad-1 is inexact (a dependency blocker for the partition); the unapplied credit note rides along
    assert _bucket(census, "invoice", "bad-1").bucket == EXCLUDED_PARTITION
    assert _bucket(census, "invoice", "cn-1").bucket == EXCLUDED_PARTITION
    (excluded,) = census.excluded
    assert excluded.partition == ("cust-2", "NGN")
    assert census.carry_partitions == (("cust-1", "NGN"),)


def test_unapplied_credit_note_does_not_exclude_its_partition_and_is_listed_for_the_work_queue() -> None:
    invoices = [
        _inv("open-1", total="100.00"),
        _inv("cn-1", total="-20.00", invoice_type="CREDIT_NOTE"),
        _inv("cn-only", customer="cust-9", total="-5.00", invoice_type="CREDIT_NOTE"),
        _inv("cn-zero", total="0.00", invoice_type="CREDIT_NOTE"),
    ]
    census = _census(invoices)
    assert _bucket(census, "invoice", "cn-1").bucket == UNAPPLIED_CREDIT_NOTE
    assert _bucket(census, "invoice", "cn-only").bucket == UNAPPLIED_CREDIT_NOTE
    assert _bucket(census, "invoice", "cn-zero").bucket == CLOSED  # residual 0: history
    assert census.carry_partitions == (("cust-1", "NGN"),) and census.excluded == ()
    assert census.credit_note_only_partitions == (("cust-9", "NGN"),)  # nothing to carry: no account minted
    receipt = census.receipt()
    assert [(n["key"], n["document_number"], n["gross_minor"]) for n in receipt["unapplied_credit_notes"]] == [
        ("cn-1", "CN-1", -2000),
        ("cn-only", "CN-ONLY", -500),
    ]


def test_totals_by_origin_tie_to_the_source_and_origin_is_provenance() -> None:
    invoices = [
        _inv("n-1", total="10.00"),
        _inv("s-1", total="20.00", dotmac_sub_id="sub-ext"),
        _inv("x-1", total="30.00", splynx_id="spx"),
    ]
    census = _census(invoices)
    totals = census.totals()
    assert {o: totals[o][CARRY_ITEM]["residual_minor"] for o in totals} == {
        "native": 1000,
        "dotmac_sub": 2000,
        "splynx": 3000,
    }
    assert census.receipt()["buckets"][CARRY_ITEM] == 3


def test_sha_is_deterministic_bound_to_the_snapshot_and_to_every_bucket() -> None:
    invoices = [_inv("a"), _inv("b", total="5.00")]
    one, two = _census(invoices), _census(list(reversed(invoices)))
    assert one.sha256 == two.sha256  # input order is not identity
    assert _census(invoices, sha="d" * 64).sha256 != one.sha256
    assert _census([_inv("a"), _inv("b", total="5.01")]).sha256 != one.sha256


def test_missing_provenance_column_refuses_the_census() -> None:
    row = _inv("a")
    del row["dotmac_sub_id"]
    with pytest.raises(OpeningTranslationError, match="lacks dotmac_sub_id"):
        _census([row])
    pay = _pay("p", "1.00")
    del pay["splynx_id"]
    with pytest.raises(OpeningTranslationError, match="lacks splynx_id"):
        _census([_inv("a")], [pay])


def test_duplicate_ids_and_bad_snapshot_sha_refuse() -> None:
    with pytest.raises(CensusError, match="duplicate invoice"):
        _census([_inv("a"), _inv("a")])
    with pytest.raises(CensusError, match="duplicate payment"):
        _census([_inv("a")], [_pay("p", "1.00"), _pay("p", "1.00")])
    with pytest.raises(CensusError, match="64 lowercase hex"):
        _census([_inv("a")], sha="nothex")


def test_partition_proof_fails_when_a_row_is_dropped_or_a_total_drifts() -> None:
    invoices = [_inv("a"), _inv("b")]
    census = _census(invoices)
    with pytest.raises(CensusError, match="exactly once"):
        prove_partition(census, invoice_ids={"a", "b", "c"}, payment_ids=set(), invoices=invoices, payments=[])
    drifted = [_inv("a"), _inv("b", total="999.00")]
    with pytest.raises(CensusError, match="do not tie"):
        prove_partition(census, invoice_ids={"a", "b"}, payment_ids=set(), invoices=drifted, payments=[])


def test_allocation_of_a_payment_not_in_play_is_dropped_not_misattributed() -> None:
    # The payment is not posted by the cutoff, so its allocation never discharged anything.
    invoices = [_inv("open-1", total="100.00")]
    payments = [_pay("p-1", "40.00", posted=False)]
    census = _census(invoices, payments, [alloc("p-1", "open-1", "40.00")])
    assert _bucket(census, "invoice", "open-1").residual_minor == 10000
    assert census.excluded == ()


def test_inexact_amount_excludes_the_partition_and_is_counted_in_the_proof_as_zero() -> None:
    census = _census([_inv("a", total="1.005")])
    assert _bucket(census, "invoice", "a").bucket == EXCLUDED_PARTITION
    assert census.excluded[0].blockers == (("a", "amount_not_exact"),)


def test_collection_block_lists_carried_partitions_with_a_left_out_credit_note() -> None:
    census = _census(
        [
            _inv("open-1", total="100.00"),
            _inv("cn-1", total="-20.00", invoice_type="CREDIT_NOTE"),
            _inv("cn-only", customer="cust-9", total="-5.00", invoice_type="CREDIT_NOTE"),
        ]
    )
    assert census.collection_block() == [
        {"customer": "cust-1", "currency": "NGN", "billing_account": None, "legacy_key": "cn-1"}
    ]  # cust-9 carries nothing: no billing account, nothing to collect
    block = census.collection_block({("cust-1", "NGN"): "acct-1"})
    assert block[0]["billing_account"] == "acct-1" and census.receipt()["collection_block"] == census.collection_block()


def test_nothing_legacy_recorded_is_dropped_silently() -> None:
    invoices = [
        _inv("approved-1", posted=False, status="APPROVED"),  # non-draft, no posted journal, may owe
        _inv("void-1", posted=False, status="VOID"),
        _inv("rev-1", posted=False, status="PAID", reversed=True),
        _inv("late-1", total="40.00", invoice_date=datetime(2026, 2, 1, tzinfo=UTC)),  # posted by D, dated after
    ]
    payments = [
        _pay("p-unposted", "5.00", posted=False, status="PENDING"),
        _pay("p-void", "6.00", posted=False, status="VOID"),
        _pay("p-late", "7.00", payment_date=datetime(2026, 2, 2, tzinfo=UTC)),
    ]
    census = _census(invoices, payments)
    by = {(r.kind, r.key): (r.bucket, r.reason) for r in census.rows}
    assert by[("invoice", "approved-1")] == (NOT_RECEIVABLE, "unposted_approved")
    assert by[("invoice", "void-1")] == (CLOSED, "void") and by[("invoice", "rev-1")] == (CLOSED, "reversed")
    assert by[("invoice", "late-1")] == (NOT_RECEIVABLE, "posted_after_cutoff")
    assert by[("payment", "p-unposted")] == (NOT_RECEIVABLE, "unposted_pending")
    assert by[("payment", "p-void")] == (CLOSED, "void")
    assert by[("payment", "p-late")] == (NOT_RECEIVABLE, "posted_after_cutoff")
    receipt = census.receipt()
    assert sorted((d["kind"], d["key"], d["status"]) for d in receipt["unposted_documents"]) == [
        ("invoice", "approved-1", "APPROVED"),
        ("payment", "p-unposted", "PENDING"),
    ]
    assert sorted(d["key"] for d in receipt["posted_after_cutoff"]) == ["late-1", "p-late"]
