"""Register source `ar_census`: the AR census receipt becomes open work items (E35 / E36a / E36b), by sha."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from ar_opening_support import CUTOFF, SNAPSHOT, alloc, invoice, payment

from erp_v2.importer.ar_attribution import attribution_sha256
from erp_v2.importer.ar_census import build_census
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import LedgerRefused


def _receipt():
    invoices = [
        invoice("open-1", total="100.00", posted=True, balance_due=Decimal("85.00")),  # stored-balance variance
        invoice("cn-1", total="-20.00", invoice_type="CREDIT_NOTE", posted=True),
        invoice("bad", customer="cust-2", total="10.005", posted=True),  # inexact: whole partition out
        invoice("draft-1", customer="cust-3", status="DRAFT", posted=False),
        invoice("sub-1", customer="cust-3", status="SUBMITTED", posted=False),
        invoice("cn-only", customer="cust-4", total="-5.00", invoice_type="CREDIT_NOTE", posted=True),
    ]
    payments = [payment("p-1", "40.00", posted=True)]
    allocations = [alloc("p-1", "open-1", "10.00")]
    census = build_census(
        snapshot_sha256="c" * 64,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=payments,
        allocations=allocations,
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    body = json.loads(json.dumps(census.receipt(), default=str))
    body["gl_attribution"] = {
        "parties": [],
        "held": [{"reason": "ar_unapplied_credit_note", "legacy_key": "cn-1", "amount_minor": -2000}],
        "unattributed": [],
        "shared_ar_journal": [],
    }
    body["gl_attribution_sha256"] = attribution_sha256(body["gl_attribution"])
    return census, body


def _resha(body):
    body["gl_attribution_sha256"] = attribution_sha256(body["gl_attribution"])
    return body


def test_receipt_becomes_open_work_items_by_class_and_key():
    census, body = _receipt()
    facts = register.from_ar_census(body)
    by_key = {(f.class_code, f.legacy_key): f for f in facts}
    assert all(f.source_kind == "ar_census" and f.terminal_outcome is None for f in facts)  # every item opens
    assert all(f.evidence_sha256 == census.sha256 for f in facts)
    assert ("E35", "partition:cust-2:NGN") in by_key
    assert by_key[("E35", "partition:cust-2:NGN")].detail["holding"] == "H-SUB"
    assert ("E35", "ar_unapplied_credit_note:cn-1") in by_key and ("E35", "ar_unapplied_credit_note:cn-only") in by_key
    credit = by_key[("E35", "ar_unapplied_credit_note:cn-1")].detail
    assert credit["document_number"] == "CN-1" and credit["amount_minor"] == -2000
    assert credit["gl_signed_minor"] == -2000 and credit["currency"] == "NGN"
    assert by_key[("E35", "doc:draft-1")].detail == {"holding": "H-DOC", "status": "DRAFT", "amount_minor": 100000}
    assert by_key[("E35", "doc:sub-1")].detail["status"] == "SUBMITTED"
    (variance,) = [f for f in facts if f.class_code == "E36b"]
    assert (
        variance.legacy_key == "variance:open-1"
        and variance.detail["amount_minor"] == -500
        and variance.detail["basis"] == "memo"
    )


def test_the_census_carries_the_credit_note_partition_while_the_inexact_one_is_excluded():
    census, _ = _receipt()
    assert ("cust-1", "NGN") in census.carry_partitions and ("cust-2", "NGN") not in census.carry_partitions
    assert census.credit_note_only_partitions == (("cust-4", "NGN"),)


def test_gl_attribution_gaps_are_e36a_items():
    _, body = _receipt()
    body["gl_attribution"] = {
        "held": [],
        "parties": [
            {"customer": "cust-1", "currency": "NGN", "custgap_minor": 0},
            {"customer": "cust-9", "currency": "NGN", "custgap_minor": 5},
        ],
        "unattributed": [
            {"gljournal_id": "j-1", "amount_minor": 7},
            {"gljournal_id": "j-2", "amount_minor": 1, "shared": True},
        ],
    }
    facts = [f for f in register.from_ar_census(_resha(body)) if f.class_code == "E36a"]
    assert sorted(f.legacy_key for f in facts) == ["custgap:cust-9:NGN", "gljournal:j-1", "gljournal:j-2"]
    assert [f.detail["shared"] for f in facts if f.legacy_key.startswith("gljournal")] == [False, True]
    body["gl_attribution"] = "x"
    with pytest.raises(LedgerRefused):
        register.from_ar_census(body)


def test_a_census_without_gl_attribution_is_refused():
    _, body = _receipt()
    del body["gl_attribution"]
    with pytest.raises(LedgerRefused) as caught:
        register.from_ar_census(body)
    assert caught.value.code == "ar_census_without_gl_attribution"


def test_posted_after_cutoff_rows_become_e35_row_items():
    _, body = _receipt()
    body["posted_after_cutoff"] = [
        {"kind": "invoice", "key": "late-1", "partition": ["cust-1", "NGN"], "gross_minor": 4000}
    ]
    body["gl_attribution"]["held"].append({"reason": "row_after_cutoff", "legacy_key": "late-1", "amount_minor": 4000})
    (fact,) = [f for f in register.from_ar_census(_resha(body)) if f.legacy_key == "row:late-1"]
    assert fact.class_code == "E35" and fact.detail["gl_signed_minor"] == 4000 and fact.detail["currency"] == "NGN"


def test_wrong_receipt_kind_is_refused_and_the_kind_is_registered():
    with pytest.raises(LedgerRefused):
        register.from_ar_census({"kind": "import-itemised", "queue": []})
    with pytest.raises(LedgerRefused):
        register.from_ar_census([])
    assert register.SRC_AR_CENSUS in register.RECEIPT_KINDS


def test_the_same_receipt_yields_the_same_fingerprints():
    _, body = _receipt()
    assert [f.fingerprint for f in register.from_ar_census(body)] == [
        f.fingerprint for f in register.from_ar_census(body)
    ]


def test_an_edited_attribution_block_is_refused_by_its_sha():
    _, body = _receipt()
    body["gl_attribution"]["held"][0]["amount_minor"] = 1  # edited after the census
    with pytest.raises(LedgerRefused) as caught:
        register.from_ar_census(body)
    assert caught.value.code == "gl_attribution_sha_mismatch"
    del body["gl_attribution_sha256"]
    with pytest.raises(LedgerRefused):
        register.from_ar_census(body)
