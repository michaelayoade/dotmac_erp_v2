"""B-9 migration ledger, pure parts: opening proof, bridge partition, register adapters, migration shape."""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from erp_v2.migration_ledger import bridge, opening, register
from erp_v2.migration_ledger.common import LedgerRefused, canonical_sha, strict_json

SHA = "a" * 64
D = Decimal


def refusal(fn, *a, **kw) -> str:
    with pytest.raises(LedgerRefused) as caught:
        fn(*a, **kw)
    return caught.value.code


# --- opening ------------------------------------------------------------------------------------------------------


def tb_file(rows):
    return {
        "version": 1,
        "as_of": "2025-12-31",
        "rows": [{"account_code": c, "debit": d, "credit": k} for c, d, k in rows],
    }


GOOD_TB = [
    ("1000", "500.00", "0"),
    ("1400", "300.00", "0"),
    ("1410", "50.00", "0"),
    ("2000", "0", "400.00"),
    ("2040", "0", "100.00"),
    ("3100", "0", "350.00"),
]
SUBLEDGER = {
    "version": 1,
    "openings": {
        code: {"net": net, "source_receipt_sha256": SHA}
        for code, net in (("2000", "-400.00"), ("2040", "-100.00"), ("1410", "50.00"), ("1400", "300.00"))
    },
}


def inputs(tb_rows=GOOD_TB, sub=SUBLEDGER, ias8=()):
    as_of, tb = opening.parse_tb(tb_file(tb_rows), SHA)
    return opening.OpeningInputs(as_of, tb, SHA, opening.parse_subledger(sub), SHA, tuple(ias8), SHA)


def ias8_item(item_id, code, amount):
    return opening.Ias8Item(item_id, code, D(amount), SHA, "restate")


def test_opening_zero_sum_and_control_tie_pass():
    proof = opening.opening_proof(inputs())
    assert proof["ok"] and proof["zero_sum"] and proof["tb_net_total"] == "0.000000"
    opening.require_proof(proof)


def test_opening_refuses_a_trial_balance_that_does_not_sum_to_zero():
    rows = [r for r in GOOD_TB if r[0] != "3100"] + [("3100", "0", "350.01")]
    proof = opening.opening_proof(inputs(rows))
    assert not proof["ok"] and not proof["zero_sum"]
    assert refusal(opening.require_proof, proof) == "opening_tb_not_zero_sum"


def test_a_sub_kobo_imbalance_is_refused_too():
    rows = [r for r in GOOD_TB if r[0] != "3100"] + [("3100", "0", "350.000001")]
    assert not opening.opening_proof(inputs(rows))["zero_sum"]


def test_opening_refuses_when_a_control_account_differs_from_its_subledger():
    sub = json.loads(json.dumps(SUBLEDGER))
    sub["openings"]["2000"]["net"] = "-399.00"  # the subledger holds 1.00 less than the TB
    proof = opening.opening_proof(inputs(sub=sub))
    assert not proof["ok"] and proof["zero_sum"]
    assert proof["control_accounts"]["2000"]["difference"] == "-1.000000"
    assert refusal(opening.require_proof, proof) == "opening_control_not_subledger"


def test_an_itemised_ias8_movement_closes_the_control_gap_and_only_exactly():
    sub = json.loads(json.dumps(SUBLEDGER))
    sub["openings"]["2000"]["net"] = "-399.00"
    # the IAS 8 journal moves 2000 by +1.00 net (a debit), so TB -400 + 1 = -399 = the subledger
    assert opening.opening_proof(inputs(sub=sub, ias8=[ias8_item("I1", "2000", "1.00")]))["ok"]
    assert not opening.opening_proof(inputs(sub=sub, ias8=[ias8_item("I1", "2000", "0.99")]))["ok"]
    assert not opening.opening_proof(inputs(sub=sub, ias8=[ias8_item("I1", "2000", "-1.00")]))["ok"]


def test_every_control_account_must_have_a_declared_subledger_opening():
    sub = json.loads(json.dumps(SUBLEDGER))
    del sub["openings"]["1400"]
    assert refusal(opening.parse_subledger, sub) == "invalid_subledger_schema"


def test_a_missing_control_account_in_the_tb_counts_as_zero_and_must_tie():
    rows = [r for r in GOOD_TB if r[0] != "1410"] + [("3100", "0", "300.00")]
    rows = [r for r in rows if not (r[0] == "3100" and r[2] == "350.00")]
    assert not opening.opening_proof(inputs(rows))["ok"]  # 1410 subledger is 50, TB has none


def test_tb_and_ias8_parsing_is_strict():
    assert refusal(opening.parse_tb, tb_file([*GOOD_TB, ("1000", "1", "0")]), SHA) == "tb_duplicate_account"
    assert refusal(opening.parse_tb, tb_file([("1000", "1", "1")]), SHA) == "invalid_tb_row"
    assert refusal(opening.parse_tb, tb_file([("1000", "-1", "0")]), SHA) == "invalid_tb_row"
    assert refusal(strict_json, b'{"a": 1.5}') == "json_float_refused"
    assert refusal(strict_json, b'{"a": 1, "a": 2}') == "duplicate_json_key"
    item = {"item_id": "I", "account_code": "3100", "amount": "1", "evidence_sha256": SHA, "description": "d"}
    assert refusal(opening.parse_ias8, {"version": 1, "items": [item]}) == "ias8_account_is_retained_earnings"
    item["account_code"] = "6000"
    assert refusal(opening.parse_ias8, {"version": 1, "items": [item, item]}) == "ias8_duplicate_item"
    item["amount"] = "0"
    assert refusal(opening.parse_ias8, {"version": 1, "items": [item]}) == "ias8_zero_item"


def test_the_plan_sha_binds_every_input_and_the_proof():
    base = opening.plan_sha256(inputs(), opening.opening_proof(inputs()))
    other_rows = [("1000", "501.00", "0") if r[0] == "1000" else r for r in GOOD_TB]
    assert opening.plan_sha256(inputs(other_rows), opening.opening_proof(inputs(other_rows))) != base


# --- bridge -------------------------------------------------------------------------------------------------------


def item(item_id, bucket, ref, account, period, legacy, v2, *, rule=None, scope=None):
    return {
        "item_id": item_id,
        "bucket": bucket,
        "ref": ref,
        "account": account,
        "period": period,
        "rule": rule,
        "scope": scope,
        "legacy_amount": legacy,
        "v2_amount": v2,
        "evidence_sha256": SHA,
    }


def bridge_inputs(items, legacy, refs=None, rounding="6900"):
    raw = {
        "version": 1,
        "snapshot_sha256": SHA,
        "rounding_account_code": rounding,
        "universe_refs": sorted(refs if refs is not None else {i["ref"] for i in items}),
        "legacy": [{"account": a, "period": p, "net": n} for (a, p), n in legacy.items()],
        "items": items,
    }
    return bridge.parse_inputs(raw, SHA)


def compute(inp, **kw):
    return bridge.compute(
        inp, opening_net=kw.get("opening_net"), ias8=kw.get("ias8", []), opening_tb_sha=None, ias8_sha=None
    )[0]


def row(body, account, period):
    return next(r for r in body["rows"] if r["account"] == account and r["period"] == period)


BASIC_ITEMS = [
    item("1", "carry", "J1", "5000", "2026-01", "10.00", "10.00"),
    item("2", "transform", "J2", "5000", "2026-01", "20.00", "21.50", rule="T-EXP-LEGACY", scope="line"),
    item("3", "queue", "J3", "5000", "2026-01", "7.00", "0"),
]
BASIC_LEGACY = {("5000", "2026-01"): "37.00"}


def test_partition_proof_every_ref_in_exactly_one_bucket():
    body = compute(bridge_inputs(BASIC_ITEMS, BASIC_LEGACY))
    p = body["partition"]
    assert p["ok"] and p["refs_by_bucket"] == {"carry": 1, "transform": 1, "queue": 1}
    r = row(body, "5000", "2026-01")
    # legacy 37 = carry 10 + transform(legacy) 20 + queue 7; v2 = 10 + 21.5; difference = delta 1.5 - queue 7
    assert (r["partition_gap"], r["v2_planned"], r["difference"]) == ("0.000000", "31.500000", "-5.500000")
    assert r["transform_delta"] == "1.500000" and r["queue"] == "7.000000"


def test_partition_proof_fails_for_missing_extra_and_split_refs():
    inp = bridge_inputs(BASIC_ITEMS, BASIC_LEGACY, refs={"J1", "J2", "J3", "J4"})
    assert bridge.partition_proof(inp)["refs_missing_from_bridge"] == 1 and not bridge.partition_proof(inp)["ok"]
    inp = bridge_inputs(BASIC_ITEMS, BASIC_LEGACY, refs={"J1", "J2"})
    assert bridge.partition_proof(inp)["refs_not_in_universe"] == 1 and not bridge.partition_proof(inp)["ok"]
    split = [*BASIC_ITEMS, item("4", "queue", "J1", "5000", "2026-01", "1.00", "0")]
    inp = bridge_inputs(split, {("5000", "2026-01"): "38.00"})
    assert bridge.partition_proof(inp)["refs_in_multiple_buckets"] == 1 and not compute(inp)["ready"]


def test_a_queued_amount_makes_the_bridge_not_ready_and_a_clean_one_ready():
    assert not compute(bridge_inputs(BASIC_ITEMS, BASIC_LEGACY))["ready"]  # queue is non-empty
    clean = [BASIC_ITEMS[0], BASIC_ITEMS[1]]
    assert compute(bridge_inputs(clean, {("5000", "2026-01"): "30.00"}))["ready"]


def test_there_is_no_materiality_threshold_a_one_micro_gap_is_unsignable():
    clean = [BASIC_ITEMS[0], BASIC_ITEMS[1]]
    body = compute(bridge_inputs(clean, {("5000", "2026-01"): "30.000001"}))
    assert not body["ready"] and row(body, "5000", "2026-01")["partition_gap"] == "0.000001"


def test_a_legacy_row_with_no_items_is_a_gap_not_silence():
    body = compute(bridge_inputs([], {("5000", "2026-01"): "5.00"}, refs=set()))
    assert not body["ready"] and body["partition_gap_abs_total"] == "5.000000"


def test_the_version_sha_binds_the_body_and_is_deterministic():
    a = compute(bridge_inputs(BASIC_ITEMS, BASIC_LEGACY))
    b = compute(bridge_inputs(list(reversed(BASIC_ITEMS)), BASIC_LEGACY))
    assert bridge.body_sha256(a) == bridge.body_sha256(b)
    changed = [
        *BASIC_ITEMS[:1],
        item("2", "transform", "J2", "5000", "2026-01", "20.00", "21.51", rule="T-EXP-LEGACY", scope="line"),
        BASIC_ITEMS[2],
    ]
    assert bridge.body_sha256(compute(bridge_inputs(changed, BASIC_LEGACY))) != bridge.body_sha256(a)


ROUND_ITEMS = [
    item("r1", "transform", "A", "2000", "2026-01", "-100.005", "-100.01", rule="T-ROUND-2DP", scope="header"),
    item("r2", "transform", "B", "2000", "2026-01", "-50.004", "-50.00", rule="T-ROUND-2DP", scope="header"),
    item("r3", "transform", "C", "2000", "2026-02", "-10.004", "-10.00", rule="T-ROUND-2DP", scope="header"),
    item("r4", "transform", "A", "5000", "2026-01", "100.005", "100.01", rule="T-ROUND-2DP", scope="line"),
]
ROUND_LEGACY = {
    ("2000", "2026-01"): "-150.009",
    ("2000", "2026-02"): "-10.004",
    ("5000", "2026-01"): "100.005",
}


def test_rounding_residue_is_summed_per_period_and_planned_against_the_declared_account():
    body = compute(bridge_inputs(ROUND_ITEMS, ROUND_LEGACY))
    # header deltas (v2 - legacy net): A -0.005, B +0.004 -> Jan -0.001; C +0.004 -> Feb
    assert body["rounding_residue_by_period"] == {"2026-01": "-0.001000", "2026-02": "0.004000"}
    assert row(body, "2000", "2026-01")["rounding"] == "0.001000"  # the planned correction clears 2000
    assert row(body, "6900", "2026-01")["rounding"] == "-0.001000"
    ap = row(body, "2000", "2026-01")
    assert D(ap["transform_delta"]) + D(ap["rounding"]) == 0  # the header delta is cleared, itemised, no tolerance
    assert body["ready"]  # nothing queued, no gap


def test_a_missing_rounding_account_refuses_when_there_is_a_residue():
    assert refusal(compute, bridge_inputs(ROUND_ITEMS, ROUND_LEGACY, rounding=None)) == "rounding_account_required"
    assert refusal(compute, bridge_inputs(ROUND_ITEMS, ROUND_LEGACY, rounding="2000")) == "rounding_account_invalid"
    # no residue: the account is not needed
    assert compute(bridge_inputs(BASIC_ITEMS[:2], {("5000", "2026-01"): "30.00"}, rounding=None))["ready"]


def test_bridge_inputs_are_strict():
    assert refusal(bridge_inputs, [item("1", "carry", "J", "5000", "2026-01", "1", "2")], {}) == "carry_item_differs"
    assert refusal(bridge_inputs, [item("1", "queue", "J", "5000", "2026-01", "1", "1")], {}) == "queue_item_posted"
    assert (
        refusal(bridge_inputs, [item("1", "transform", "J", "5000", "2026-01", "1", "2")], {})
        == "transform_item_needs_rule_and_scope"
    )
    dup = [item("1", "carry", "J", "5000", "2026-01", "1", "1"), item("1", "carry", "K", "5000", "2026-01", "1", "1")]
    assert refusal(bridge_inputs, dup, {}) == "bridge_duplicate_item"
    assert (
        refusal(bridge_inputs, [item("1", "carry", "J", "5000", "OPENING", "1", "1")], {})
        == "opening_period_is_ledger_carried"
    )
    bad = [item("1", "transform", "J", "5000", "2026-01", "1", "2", rule="T-ROUND-2DP", scope="header")]
    assert refusal(bridge_inputs, bad, {}) == "rounding_header_not_ap_control"


def test_the_opening_period_is_carried_by_the_ledger_and_ias8_is_itemised():
    legacy = {("1000", "OPENING"): "500.00", ("2000", "OPENING"): "-400.00"}
    body = compute(
        bridge_inputs([], legacy, refs=set()),
        opening_net={"1000": D("500"), "2000": D("-401")},
        ias8=[("2000", D("1"))],
    )
    r = row(body, "2000", "OPENING")
    assert (r["carry"], r["ias8"], r["v2_planned"], r["difference"]) == (
        "-401.000000",
        "1.000000",
        "-400.000000",
        "0.000000",
    )
    # the TB-as-reported carries -401 here, so the legacy -400 shows a partition gap: it is never netted away
    assert r["partition_gap"] == "1.000000" and not body["ready"]


# --- register -----------------------------------------------------------------------------------------------------


def held_receipt(**over):
    line = {
        "journal_id": "j1",
        "rule_id": "R-QUEUE",
        "outcome": "queued",
        "reason_code": "no_published_rule",
        "amount": "1.000000",
        "evidence_sha256": SHA,
    }
    receipt = {
        "mode": "apply",
        "bridge_lines": [line, {**line, "journal_id": "j2", "outcome": "voided", "rule_id": "R-BANK-FEE"}],
        "e17_findings": [
            {
                "finding": "E17",
                "key_sha256": SHA,
                "statement_lines": 1,
                "posted_fee_journals": 2,
                "excess_posted_fee_journals": 1,
                "posted_journal_ids_sha256": SHA,
            }
        ],
    }
    receipt.update(over)
    return receipt


def test_held_drafts_adapter_maps_queued_open_voided_resolved_and_e17_open():
    facts = {f.legacy_key: f for f in register.from_held_drafts(held_receipt())}
    assert facts["held_draft:j1"].terminal_outcome is None and facts["held_draft:j1"].class_code == "E16a"
    assert facts["held_draft:j2"].terminal_outcome == "voided"
    e17 = facts[f"e17:{SHA}"]
    assert e17.class_code == "E17" and e17.terminal_outcome is None


def test_a_plan_receipt_is_never_loaded():
    assert refusal(register.from_held_drafts, held_receipt(mode="plan")) == "receipt_not_applied"


def test_import_adapter_refuses_an_unknown_queue_reason_instead_of_guessing():
    ok = {
        "queue": [
            {"invoice_id": "i1", "reasons": ["total_mismatch_after_transform"], "lands": False, "legacy_total": "1.00"}
        ]
    }
    (fact,) = register.from_import_itemised(ok)
    assert fact.class_code == "E04" and fact.terminal_outcome is None
    ok["queue"][0]["reasons"] = ["made_up"]
    assert refusal(register.from_import_itemised, ok) == "unknown_queue_reason"


def test_fingerprint_changes_with_the_legacy_facts():
    a = register.Fact("E04", "invoice:1", "import_itemised", None, None, {"x": 1})
    assert a.fingerprint == register.Fact("E04", "invoice:1", "import_itemised", None, None, {"x": 1}).fingerprint
    assert a.fingerprint != register.Fact("E04", "invoice:1", "import_itemised", None, None, {"x": 2}).fingerprint


# --- shape --------------------------------------------------------------------------------------------------------


def test_migration_revision_and_chain():
    from erp_v2.migrate import EXPECTED_HEADS
    from erp_v2.migrations.versions import e2_0028_migration_ledger as migration

    assert migration.revision == "e2_0028_migration_ledger"
    assert migration.down_revision == "e2_0027_held_draft_decisions"  # #69 merged before this build started
    assert "e2_0028_migration_ledger" in EXPECTED_HEADS
    assert "e2_0027_held_draft_decisions" not in EXPECTED_HEADS  # linear chain: one e2 head


def test_no_estate_address_literals_in_the_package():
    root = Path(__file__).resolve().parent.parent / "src" / "erp_v2" / "migration_ledger"
    pattern = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    assert not [p.name for p in root.glob("*.py") if pattern.search(p.read_text())]


def test_canonical_sha_is_key_order_independent():
    assert canonical_sha({"a": 1, "b": 2}) == canonical_sha({"b": 2, "a": 1})
