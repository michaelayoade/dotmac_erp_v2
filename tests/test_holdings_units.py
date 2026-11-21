# ruff: noqa: E501
"""B-14 holdings, pure units: itemisation, signs, shared journals, census reasons, plan sha, refusal codes, the retry
wrapper and the 'only erp_v2/holdings builds the four kinds' guard. No database."""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from erp_v2.holdings import attribution as attr
from erp_v2.holdings import billing, release
from erp_v2.holdings.attribution import Explained, GlAttribution, Party, UnattributedJournal, itemise
from erp_v2.holdings.errors import HoldingRefused
from erp_v2.holdings.models import POSTING_KINDS, holding_document_id, release_document_id, t1_document_id
from erp_v2.migration_ledger.common import LedgerRefused, canonical_sha

D = Decimal
SHA = "a" * 64


def party(k):
    return Party("supplier", k)


def attribution(**kw):
    base = dict(code="2000", currency="NGN", gl={}, explained={}, unattributed=(), opening_residue=D(0),
                source_receipt_sha256=SHA)  # fmt: skip
    base.update(kw)
    return GlAttribution(**base)


def items_for(att, sub, *, gl_total=None, sub_total=None):
    return itemise(
        attribution=att,
        sub=sub,
        gl_total=att.gl_total() if gl_total is None else gl_total,
        sub_total=sum(sub.values(), D(0)) if sub_total is None else sub_total,
        holding_account_code="2990",
    )


def test_itemisation_sums_exactly_to_x_with_explained_gap_unattributed_and_shared():
    a, b = party("A"), party("B")
    att = attribution(
        gl={a: D("-600"), b: D("-300")},
        explained={a: (Explained("E02", "refused_admission", "inv-1", D("-60")),)},
        unattributed=(UnattributedJournal("j1", D("-100")), UnattributedJournal("s1", D("-100"), shared=True)),
    )
    out = items_for(att, {a: D("-500"), b: D("-300")})
    assert sum(i.amount for i in out) == D("-1100") - D("-800")
    keys = {(i.class_code, i.legacy_key): i.amount for i in out}
    assert keys == {
        ("E02", "held:2000:NGN:inv-1"): D("-60"),
        ("E37", "partygap:2000:NGN:A"): D("-40"),
        ("E37", "gljournal:2000:NGN:j1"): D("-100"),
        ("E37", "sharedjournal:2000:NGN:s1"): D("-100"),
    }


def test_a_shared_journal_is_one_item_never_split_across_parties():
    att = attribution(unattributed=(UnattributedJournal("s9", D("-50"), shared=True),))
    out = items_for(att, {})
    assert [(i.legacy_key, i.party, i.amount) for i in out] == [("sharedjournal:2000:NGN:s9", None, D("-50"))]


def test_party_gap_class_is_e36a_for_ar_and_prepayment_otherwise_e37():
    for code, klass in (("1400", "E36a"), ("prepayment", "E36a"), ("2000", "E37"), ("1410", "E37")):
        att = attribution(code=code, gl={party("A"): D("10")})
        (only,) = items_for(att, {})
        assert only.class_code == klass


def test_a_party_with_only_subledger_gets_a_gap_item_of_the_opposite_sign():
    (only,) = items_for(attribution(), {party("Z"): D("-25")}, gl_total=D(0))
    assert only.amount == D("25")


def test_zero_explained_and_zero_remainders_make_no_items():
    a = party("A")
    att = attribution(gl={a: D("-10")}, explained={a: (Explained("E02", "x", "k", D(0)),)})
    assert items_for(att, {a: D("-10")}) == []


def test_unitemised_when_the_receipt_misses_a_journal():
    att = attribution(gl={party("A"): D("-10")})
    with pytest.raises(HoldingRefused) as e:
        items_for(att, {party("A"): D("-10")}, gl_total=D("-30"))  # the ledger holds 20 the receipt does not
    assert e.value.code == "holding_unitemised"


def test_the_opening_residue_is_its_own_item():
    (only,) = items_for(attribution(opening_residue=D("-7")), {})
    assert (only.class_code, only.legacy_key, only.reason) == ("E37", "opening:2000:NGN", "opening_residue")


def test_an_overlong_key_refuses():
    with pytest.raises(HoldingRefused) as e:
        items_for(attribution(gl={party("x" * 300): D("1")}), {})
    assert e.value.code == "holding_item_key_too_long"


# --- census ----------------------------------------------------------------------------------------------------


def census_body(**kw):
    body = {
        "parties": [
            {"customer": "C1", "currency": "NGN", "gl_minor": 100000, "carried_minor": 90000, "cash_lot_minor": 20000},
            {"customer": "C2", "currency": "NGN", "gl_minor": 50000, "carried_minor": 50000, "cash_lot_minor": 0},
        ],
        "held": [
            {"reason": "partition_held:payment_overallocated", "legacy_key": "partition:C1:NGN", "amount_minor": 25000,
             "customer": "C1", "currency": "NGN"},
            {"reason": "ar_unapplied_credit_note", "legacy_key": "row:cn1", "amount_minor": -1000,
             "customer": "C2", "currency": "NGN"},
            {"reason": "doc_unposted", "legacy_key": "doc:d1", "amount_minor": 0, "customer": "C2", "currency": "NGN"},
        ],
        "unattributed": [{"gljournal_id": "j1", "amount_minor": 3000}, {"gljournal_id": "s1", "amount_minor": 500, "shared": True}],
        "shared_ar_journal": [{"gljournal_id": "s1"}],
    }  # fmt: skip
    body.update(kw)
    return body


def test_census_attribution_1400_adds_the_cash_lot_and_prepayment_is_its_negative():
    c = attr.parse_census(census_body(), SHA)
    one = attr.census_attribution(c, code="1400", currency="NGN", functional="NGN")
    assert one.gl[Party("customer", "C1")] == D("1200.00")  # 1000.00 + the T1 cash lot 200.00
    assert one.gl[Party("customer", "C2")] == D("500.00")
    pre = attr.census_attribution(c, code="prepayment", currency="NGN", functional="NGN")
    assert pre.gl[Party("customer", "C1")] == D("-200.00")
    assert pre.unattributed == () and pre.explained == {}


def test_the_credit_note_reason_is_distinct_and_doc_unposted_is_not_a_holding_item():
    c = attr.parse_census(census_body(), SHA)
    att = attr.census_attribution(c, code="1400", currency="NGN", functional="NGN")
    c1 = att.explained[Party("customer", "C1")]
    c2 = att.explained[Party("customer", "C2")]
    assert [(e.class_code, e.reason) for e in c1] == [("E35", "partition_held:payment_overallocated")]
    assert [(e.class_code, e.reason, e.amount) for e in c2] == [("E35", "ar_unapplied_credit_note", D("-10.00"))]
    assert all(e.reason != "doc_unposted" for e in c1 + c2)


def test_census_shared_unattributed_journals_become_shared_items_and_the_total_is_exact():
    c = attr.parse_census(census_body(), SHA)
    assert c.gl_total_minor("NGN", "NGN") == 100000 + 50000 + 3000 + 500
    att = attr.census_attribution(c, code="1400", currency="NGN", functional="NGN")
    assert {(u.journal_id, u.shared) for u in att.unattributed} == {("j1", False), ("s1", True)}


def test_an_unknown_census_reason_and_a_held_item_without_its_party_refuse():
    bad = census_body(
        held=[{"reason": "mystery", "legacy_key": "k", "amount_minor": 5, "customer": "C1", "currency": "NGN"}]
    )
    with pytest.raises(HoldingRefused) as e:
        attr.census_attribution(attr.parse_census(bad, SHA), code="1400", currency="NGN", functional="NGN")
    assert e.value.code == "census_reason_unknown"
    noparty = census_body(held=[{"reason": "ar_unapplied_credit_note", "legacy_key": "k", "amount_minor": 5}])
    with pytest.raises(HoldingRefused) as e:
        attr.census_attribution(attr.parse_census(noparty, SHA), code="1400", currency="NGN", functional="NGN")
    assert e.value.code == "census_held_party_unknown"


def test_a_malformed_census_refuses():
    for broken in ({"parties": [], "held": [], "unattributed": []}, census_body(parties=[{"customer": "C"}])):
        with pytest.raises(HoldingRefused) as e:
            attr.parse_census(broken, SHA)
        assert e.value.code == "invalid_census"


def test_generic_gl_attribution_receipt_round_trips_and_is_strict():
    raw = json.dumps(
        {"version": 1, "code": "2000", "currency": "NGN",
         "parties": [{"kind": "supplier", "key": "A", "gl": "-10.00"}],
         "held": [{"kind": "supplier", "key": "A", "class_code": "E02", "reason": "r", "legacy_key": "k", "amount": "-1.00"}],
         "unattributed": [{"journal_id": "j", "amount": "-2.00", "shared": False}], "opening_residue": "0.00"}
    ).encode()  # fmt: skip
    att, sha = attr.parse_gl_attribution(raw)
    assert att.gl_total() == D("-12.00") and len(sha) == 64
    with pytest.raises(LedgerRefused):
        attr.parse_gl_attribution(raw.replace(b'"opening_residue": "0.00"', b'"opening_residue": 0'))
    with pytest.raises(LedgerRefused):
        attr.parse_gl_attribution(b'{"version": 1}')


# --- identities, sign rules, plan sha ----------------------------------------------------------------------------


def test_identities_are_deterministic_and_distinct():
    assert holding_document_id("w1", "2000", "NGN") == holding_document_id("w1", "2000", "NGN")
    assert holding_document_id("w1", "2000", "NGN") != holding_document_id("w1", "1400", "NGN")
    assert holding_document_id("w1", "2000", "NGN") != holding_document_id("w2", "2000", "NGN")
    assert t1_document_id("w1", "NGN") != holding_document_id("w1", "1400", "NGN")
    assert release_document_id("x", 1) != release_document_id("x", 2)


def test_plan_sha_is_stable_and_order_independent_for_keys():
    a = {"b": 1, "a": {"y": 2, "x": 3}}
    b = {"a": {"x": 3, "y": 2}, "b": 1}
    assert canonical_sha(a) == canonical_sha(b)
    assert canonical_sha(a) != canonical_sha({**a, "b": 2})


def test_journal_lines_are_gross_credit_control_debit_holding_for_positive_items():
    from uuid import uuid4

    control, holding = uuid4(), uuid4()
    from erp_v2.holdings import posting

    lines = posting.gross_lines(control, holding, D("30"), D("10"), label="x")
    assert [(ln.account_ref == str(holding), ln.debit, ln.credit) for ln in lines] == [
        (True, D("30"), D(0)), (False, D(0), D("30")), (False, D("10"), D(0)), (True, D(0), D("10")),
    ]  # fmt: skip
    only_positive = posting.gross_lines(control, holding, D("5"), D(0), label="x")
    assert len(only_positive) == 2 and sum(ln.debit for ln in only_positive) == sum(ln.credit for ln in only_positive)


def test_the_default_billing_reader_refuses_r1_unavailable_until_an_erp_adapter_is_bound():
    """a13 ships R1 in ``dotmac_billing.control_position``, but no ERP adapter is bound to it by the S9 composition:
    the default reader still refuses (an unbound adapter must never read as zero)."""
    reader = billing.default_reader()
    with pytest.raises(HoldingRefused) as e:
        reader.r1(None)  # type: ignore[arg-type]
    assert e.value.code == "r1_unavailable"


def test_default_owner_adapters_refuse_with_the_specified_codes():
    adapters = release.default_adapters()
    expectations = {
        "2000": "release_owner_unbound:2000", "1410": "release_owner_unbound:1410",
        "2040": "release_owner_unavailable", "2045": "release_owner_unavailable",
        "1400": "ar_release_requires_r8", "prepayment": "ar_release_requires_r8",
    }  # fmt: skip
    assert set(adapters) == set(expectations)
    for code, refusal in expectations.items():
        with pytest.raises(HoldingRefused) as e:
            adapters[code].preview(None, None, command={})  # type: ignore[arg-type]
        assert e.value.code == refusal


def test_serialization_failures_retry_up_to_three_times_then_refuse():
    class Orig(Exception):
        sqlstate = "40001"

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("stmt", {}, Orig())
        return {"ok": True}

    assert release.with_retry(flaky) == {"ok": True} and calls["n"] == 3

    def always():
        calls["n"] += 1
        raise OperationalError("stmt", {}, Orig())

    calls["n"] = 0
    with pytest.raises(HoldingRefused) as e:
        release.with_retry(always)
    assert e.value.code == "serialization_retries_exhausted" and calls["n"] == 3

    class Other(Exception):
        sqlstate = "23505"

    def hard():
        raise OperationalError("stmt", {}, Other())

    with pytest.raises(OperationalError):
        release.with_retry(hard)


def test_only_the_holdings_package_builds_the_four_migration_kinds():
    src = Path(__file__).resolve().parent.parent / "src" / "erp_v2"
    # S8: the ONE claim-family reclass kind is built by claims_opening/reclass.py (from the holdings constants, never a literal)
    allowed = {
        src / "holdings",
        src / "posting" / "rules.py",
        src / "migrations",
        src / "claims_opening" / "reclass.py",
    }
    literal = re.compile(r"""["'](%s)["']""" % "|".join(POSTING_KINDS))
    owner = re.compile(r"""["']erp\.migration["']""")
    offenders = []
    for path in src.rglob("*.py"):
        if any(path == a or a in path.parents for a in allowed):
            continue
        body = path.read_text()
        if literal.search(body) or owner.search(body):
            offenders.append(str(path.relative_to(src)))
    assert offenders == []


def test_the_posting_rules_carry_exactly_the_four_reviewed_entries():
    from erp_v2.posting.rules import POLICIES

    mine = {k for k in POLICIES if k[0] == "erp.migration"}
    assert mine == {("erp.migration", kind) for kind in POSTING_KINDS}


def test_credit_notes_key_on_the_reason_and_a_disagreeing_prefix_refuses():
    body = census_body(held=[{"reason": "ar_unapplied_credit_note", "legacy_key": "cn7", "amount_minor": -500,
                              "customer": "C2", "currency": "NGN"}])  # fmt: skip
    att = attr.census_attribution(attr.parse_census(body, SHA), code="1400", currency="NGN", functional="NGN")
    (e,) = att.explained[Party("customer", "C2")]
    assert (e.class_code, e.reason, e.source_key) == ("E35", "ar_unapplied_credit_note", "ar_unapplied_credit_note:cn7")
    clash = census_body(held=[{"reason": "row_after_cutoff", "legacy_key": "ar_unapplied_credit_note:cn7",
                               "amount_minor": -500, "customer": "C2", "currency": "NGN"}])  # fmt: skip
    with pytest.raises(HoldingRefused) as err:
        attr.census_attribution(attr.parse_census(clash, SHA), code="1400", currency="NGN", functional="NGN")
    assert err.value.code == "census_reason_mismatch"


def test_every_census_reason_maps_to_its_register_key():
    assert attr.census_register_key("partition_held:x", "partition:C1:NGN") == "partition:C1:NGN"
    assert attr.census_register_key("ar_unapplied_credit_note", "cn1") == "ar_unapplied_credit_note:cn1"
    assert attr.census_register_key("row_after_cutoff", "r1") == "row:r1"


def test_items_link_back_to_their_source_exception():
    a = party("A")
    att = attribution(gl={a: D("-60")}, explained={a: (Explained("E02", "r", "inv-1", D("-60")),)})
    (item,) = items_for(att, {a: D(0)})
    assert item.detail()["origin"] == {"class": "E02", "legacy_key": "inv-1"}


def test_the_real_72_census_output_is_consumed_by_the_b14_adapter():
    """F5: feed #72's real ``build_attribution`` output (not a hand-written fixture) through parse_census."""
    from test_ar_attribution import _attr

    _census, receipt = _attr()
    census = attr.parse_census(receipt, SHA, SHA)
    att = attr.census_attribution(census, code="1400", currency="NGN", functional="NGN")
    reasons = {(e.class_code, e.reason) for es in att.explained.values() for e in es}
    assert ("E35", "ar_unapplied_credit_note") in reasons
    assert any(r.startswith("partition_held:") for _c, r in reasons)
    # the receipt's own invariant carries through: parties' GL + unattributed == the legacy 1400 net
    assert census.gl_total_minor("NGN", "NGN") == receipt["legacy_1400_minor_by_currency"]["NGN"]
    # every held row names its source register key under the #72 key scheme
    for es in att.explained.values():
        for e in es:
            assert e.source_key.startswith(("partition:", "ar_unapplied_credit_note:", "row:"))
