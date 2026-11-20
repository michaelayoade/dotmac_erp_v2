"""Per-customer legacy 1400 attribution: the census receipt fields B-14 consumes (B14-B15-SPEC B14.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from ar_opening_support import CUTOFF, SNAPSHOT, alloc, invoice, payment

from erp_v2.importer.ar_attribution import build_attribution
from erp_v2.importer.ar_census import CensusError, build_census

C1, C2, C3, C4 = ("cust-1", "NGN"), ("cust-2", "NGN"), ("cust-3", "NGN"), ("cust-4", "NGN")


def _census():
    invoices = [
        invoice("inv-1", total="100.00", posted=True),  # open 60.00 after p-1
        invoice("cn-1", total="-20.00", invoice_type="CREDIT_NOTE", posted=True),  # unapplied: out of the manifest
        invoice("bad", customer="cust-2", total="10.005", posted=True),  # inexact -> whole partition held
        invoice("ok-2", customer="cust-2", total="50.00", posted=True),
        invoice("draft-1", customer="cust-3", status="DRAFT", posted=False),
        invoice("inv-4", customer="cust-4", total="30.00", posted=True),
    ]
    payments = [payment("p-1", "50.00", posted=True), payment("p-4", "45.00", customer="cust-4", posted=True)]
    allocations = [alloc("p-1", "inv-1", "40.00")]
    census = build_census(
        snapshot_sha256="d" * 64,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=payments,
        allocations=allocations,
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    types = {i["invoice_id"]: i["invoice_type"] for i in invoices}
    return census, types


# legacy 1400 journals: (journal, net). inv-1 +100, p-1 -50, cn-1 -20, bad +10.00 (sub-kobo is refused elsewhere),
# ok-2 +50, inv-4 +30, p-4 -45, a manual journal nobody owns +7.00, a journal two customers share +3.00
LINES = [
    ("j-inv-1", Decimal("100.00")),
    ("j-p-1", Decimal("-50.00")),
    ("j-cn-1", Decimal("-20.00")),
    ("j-bad", Decimal("10.00")),
    ("j-ok-2", Decimal("50.00")),
    ("j-inv-4", Decimal("30.00")),
    ("j-p-4", Decimal("-45.00")),
    ("j-manual", Decimal("7.00")),
    ("j-shared", Decimal("3.00")),
]
OWNERS = {
    "j-inv-1": [C1],
    "j-p-1": [C1],
    "j-cn-1": [C1],
    "j-bad": [C2],
    "j-ok-2": [C2],
    "j-inv-4": [C4],
    "j-p-4": [C4],
    "j-shared": [C1, C4],
}


def _attr(lines=LINES, owners=OWNERS):
    census, types = _census()
    return census, build_attribution(
        census, gl_lines=lines, journal_owners=owners, invoice_types=types, functional_currency="NGN"
    )["gl_attribution"]


def test_the_b14_invariant_sum_gl_plus_unattributed_equals_the_legacy_1400_net_per_currency():
    _, a = _attr()
    gl = sum(p["gl_minor"] for p in a["parties"])
    unattributed = sum(i["amount_minor"] for i in a["unattributed"])
    expected = sum(int(net * 100) for _j, net in LINES)
    assert gl + unattributed == expected == a["legacy_1400_minor_by_currency"]["NGN"]
    # a journal two customers share is NOT split: listed (detail) and counted once, inside unattributed
    assert a["shared_ar_journal"] == [
        {"gljournal_id": "j-shared", "amount_minor": 300, "partitions": [list(C1), list(C4)], "currency": "NGN"}
    ]
    assert {"gljournal_id": "j-shared", "amount_minor": 300, "shared": True, "currency": "NGN"} in a["unattributed"]
    assert {"gljournal_id": "j-manual", "amount_minor": 700, "currency": "NGN"} in a["unattributed"]


def test_per_party_fields_and_the_distinct_held_reasons():
    census, a = _attr()
    parties = {(p["customer"], p["currency"]): p for p in a["parties"]}
    c1 = parties[C1]
    # cust-1 carries inv-1 (residual 60.00) and p-1's remainder is 10.00 cash (50 - 40); the credit note is out
    assert (c1["gl_minor"], c1["carried_minor"], c1["cash_lot_minor"]) == (100_00 - 50_00 - 20_00, 6000, 1000)
    # GL 30.00 = carried 60 - cash lot 10 (=50) + credit note (-20): the credit note explains the difference
    assert c1["custgap_minor"] == 3000 - (6000 - 1000) - (-2000)
    assert parties[C2]["carried_minor"] == 0 and parties[C2]["cash_lot_minor"] == 0  # whole partition held
    held = {(h["reason"], h["legacy_key"]): h["amount_minor"] for h in a["held"]}
    assert held[("ar_unapplied_credit_note", "cn-1")] == -2000  # distinct reason, GL-signed (negative total)
    # an excluded partition is held WHOLE: one item equal to the party's entire GL, so nothing falls out as custgap
    assert held[("partition_held:amount_not_exact", "partition:cust-2:NGN")] == 6000
    assert parties[C2]["gl_minor"] == 6000 and parties[C2]["custgap_minor"] == 0
    assert held[("doc_unposted", "draft-1")] == 0  # no GL
    assert census.carry_partitions == (C1, C4)


def test_an_inexact_gl_amount_is_refused_not_rounded():
    census, types = _census()
    with pytest.raises(CensusError, match="gl_amount_not_exact:j-1"):
        build_attribution(
            census,
            gl_lines=[("j-1", Decimal("0.005"))],
            journal_owners={},
            invoice_types=types,
            functional_currency="NGN",
        )


def test_lines_of_one_journal_are_summed_and_a_reversal_journal_follows_its_owner():
    census, types = _census()
    a = build_attribution(
        census,
        gl_lines=[("j-a", Decimal("10.00")), ("j-a", Decimal("-4.00")), ("j-rev", Decimal("-6.00"))],
        journal_owners={"j-a": [C1], "j-rev": [C1]},
        invoice_types=types,
        functional_currency="NGN",
    )["gl_attribution"]
    assert [p["gl_minor"] for p in a["parties"] if p["customer"] == "cust-1"] == [0]
    assert a["unattributed"] == []


def test_non_functional_currency_is_refused_and_unowned_journals_are_never_forced_to_the_functional_currency():
    census, types = _census()
    for owners in ({"j-fx": [("cust-1", "USD")]},):
        with pytest.raises(CensusError, match="ar_attribution_fx_unsupported"):
            build_attribution(
                census,
                gl_lines=[("j-fx", Decimal("1.00"))],
                journal_owners=owners,
                invoice_types=types,
                functional_currency="NGN",
            )
    usd = build_census(
        snapshot_sha256="d" * 64,
        cutoff=CUTOFF,
        invoices=[invoice("u-1", currency_code="USD", posted=True)],
        payments=[],
        allocations=[],
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    with pytest.raises(CensusError, match="ar_attribution_fx_unsupported"):
        build_attribution(usd, gl_lines=[], journal_owners={}, invoice_types={}, functional_currency="NGN")


def test_posted_after_cutoff_rows_are_held_at_their_gl_signed_amount():
    invoices = [
        invoice("inv-1", total="100.00", posted=True),
        invoice("late-1", total="40.00", posted=True, invoice_date=datetime(2026, 2, 1, tzinfo=UTC)),
        invoice(
            "late-cn",
            total="25.00",
            invoice_type="CREDIT_NOTE",
            posted=True,
            invoice_date=datetime(2026, 2, 1, tzinfo=UTC),
        ),
    ]
    census = build_census(
        snapshot_sha256="d" * 64,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=[],
        allocations=[],
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    types = {i["invoice_id"]: i["invoice_type"] for i in invoices}
    a = build_attribution(
        census,
        gl_lines=[("j1", Decimal("100.00")), ("j2", Decimal("40.00")), ("j3", Decimal("-25.00"))],
        journal_owners={"j1": [C1], "j2": [C1], "j3": [C1]},
        invoice_types=types,
        functional_currency="NGN",
    )["gl_attribution"]
    held = {h["legacy_key"]: (h["reason"], h["amount_minor"]) for h in a["held"]}
    assert held == {"late-1": ("row_after_cutoff", 4000), "late-cn": ("row_after_cutoff", -2500)}
    (party,) = a["parties"]
    assert party["gl_minor"] == 11500 and party["custgap_minor"] == 0  # 100 carried + 40 - 25 explained


def test_every_held_entry_carries_its_customer_and_currency():
    _, a = _attr()
    assert a["held"], "the fixture must produce held items of every reason"
    reasons = {h["reason"].split(":")[0] for h in a["held"]}
    assert {"ar_unapplied_credit_note", "partition_held", "doc_unposted"} <= reasons
    for h in a["held"]:
        assert h["customer"] and h["currency"] == "NGN", h  # B-14 refuses census_held_party_unknown otherwise
        assert {"reason", "legacy_key", "amount_minor"} <= set(h)
    by_key = {h["legacy_key"]: h["customer"] for h in a["held"]}
    assert by_key["cn-1"] == "cust-1" and by_key["partition:cust-2:NGN"] == "cust-2" and by_key["draft-1"] == "cust-3"


def test_a_posted_after_cutoff_row_of_a_refused_party_is_not_counted_twice():
    invoices = [
        invoice("inv-1", customer="cust-2", total="100.00", posted=True),
        invoice("bad", customer="cust-2", total="10.005", posted=True),  # inexact: the partition is refused
        invoice("late-1", customer="cust-2", total="40.00", posted=True, invoice_date=datetime(2026, 2, 1, tzinfo=UTC)),
    ]
    census = build_census(
        snapshot_sha256="d" * 64,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=[],
        allocations=[],
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    a = build_attribution(
        census,
        gl_lines=[("j1", Decimal("100.00")), ("j2", Decimal("10.00")), ("j3", Decimal("40.00"))],
        journal_owners={"j1": [C2], "j2": [C2], "j3": [C2]},
        invoice_types={i["invoice_id"]: i["invoice_type"] for i in invoices},
        functional_currency="NGN",
    )["gl_attribution"]
    held = {h["legacy_key"]: h["amount_minor"] for h in a["held"]}
    assert held["late-1"] == 4000
    assert held["partition:cust-2:NGN"] == 15000 - 4000  # the whole GL minus the row held on its own
    (party,) = a["parties"]
    assert party["gl_minor"] == 15000 and sum(held.values()) == party["gl_minor"] and party["custgap_minor"] == 0


def test_the_receipt_carries_the_sha_of_the_attribution_block():
    from erp_v2.importer.ar_attribution import attribution_sha256

    census, types = _census()
    out = build_attribution(
        census, gl_lines=LINES, journal_owners=OWNERS, invoice_types=types, functional_currency="NGN"
    )
    assert out["gl_attribution_sha256"] == attribution_sha256(out["gl_attribution"])
