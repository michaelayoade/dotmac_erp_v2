"""B-9 migration ledger, pure parts: opening proof, bridge partition, register adapters, migration shape."""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from migration_ledger_support import bind_rounding

from erp_v2.migration_ledger import bridge, opening, register, subkobo
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
SUBLEDGER = {"2000": D("-400.00"), "2040": D("-100.00"), "1410": D("50.00"), "1400": D("300.00")}  # recomputed


def inputs(tb_rows=GOOD_TB, sub=SUBLEDGER, ias8=()):
    as_of, tb = opening.parse_tb(tb_file(tb_rows), SHA)
    return opening.OpeningInputs(as_of, tb, SHA, dict(sub), SHA, tuple(ias8), SHA, SHA, SHA)


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


def test_an_opening_control_difference_is_report_only_never_a_refusal():
    sub = {**SUBLEDGER, "2000": D("-399.00")}  # the subledger holds 1.00 less than the TB
    proof = opening.opening_proof(inputs(sub=sub))
    assert proof["ok"] and proof["zero_sum"] and proof["control_differences_report_only"]
    assert proof["control_accounts"]["2000"]["difference"] == "-1.000000"
    assert proof["control_differences"] == [{"account": "2000", "difference": "-1.000000", "class": "E37"}]
    opening.require_proof(proof)  # no refusal


def test_a_tied_opening_lists_no_control_differences():
    assert opening.opening_proof(inputs())["control_differences"] == []


def test_the_tb_is_generated_from_the_legacy_opening_period_and_a_differing_declared_tb_is_refused():
    legacy = {
        ("1000", "OPENING"): D("100.00"),
        ("3100", "OPENING"): D("-100.00"),
        ("1000", "2026-01"): D("5.00"),  # not the opening period
        ("2000", "OPENING"): D("0"),
    }
    tb = opening.tb_from_legacy(legacy)
    assert tb == {"1000": D("100.00"), "3100": D("-100.00")}
    body = opening.tb_file_body(date(2025, 12, 31), tb)
    as_of, parsed = opening.parse_tb(body, SHA)  # the generated file round-trips through the strict parser
    assert as_of == date(2025, 12, 31) and parsed == tb
    opening.require_declared_tb_matches(tb, dict(tb))
    assert (
        refusal(opening.require_declared_tb_matches, {**tb, "1000": D("100.01")}, tb)
        == "opening_tb_differs_from_legacy"
    )
    assert refusal(opening.require_declared_tb_matches, {"1000": D("100.00")}, tb) == "opening_tb_differs_from_legacy"
    assert opening.tb_canonical_sha(tb) == opening.tb_canonical_sha({**tb, "9999": D("0")})


def test_the_import_receipt_cutoff_is_read_both_shapes_and_must_equal_the_opening_date():
    ok = {"kind": "execute", "dry_run": False, "complete": True}
    d = date(2025, 12, 31)
    assert opening.parse_import_receipt({**ok, "history_cutoff": "2025-12-31"}) == d
    assert opening.parse_import_receipt({**ok, "history_cutoff": {"history_cutoff": "2025-12-31"}}) == d
    for bad, code in (
        ({**ok, "dry_run": True, "history_cutoff": "2025-12-31"}, "invalid_import_receipt"),
        ({**ok, "complete": False, "history_cutoff": "2025-12-31"}, "import_receipt_incomplete"),
        ({**ok}, "import_receipt_history_cutoff_missing"),
        ({**ok, "history_cutoff": "2025-13-01"}, "import_receipt_history_cutoff_invalid"),
        ([], "invalid_import_receipt"),
    ):
        assert refusal(opening.parse_import_receipt, bad) == code
    opening.require_as_of_matches_cutoff(d, d)
    assert refusal(opening.require_as_of_matches_cutoff, d, date(2025, 12, 30)) == (
        "opening_as_of_differs_from_history_cutoff"
    )


def test_ias8_is_empty_before_the_flip():
    opening.require_no_ias8_before_flip(inputs())
    assert refusal(opening.require_no_ias8_before_flip, inputs(ias8=[ias8_item("I1", "2000", "1.00")])) == (
        "ias8_not_allowed_before_flip"
    )


def test_an_itemised_ias8_movement_closes_the_control_gap_and_only_exactly():
    sub = {**SUBLEDGER, "2000": D("-399.00")}

    # the IAS 8 journal moves 2000 by +1.00 net (a debit), so TB -400 + 1 = -399 = the subledger
    def ties(amount):
        return opening.opening_proof(inputs(sub=sub, ias8=[ias8_item("I1", "2000", amount)]))["control_accounts"][
            "2000"
        ]["ties"]

    assert ties("1.00") and not ties("0.99") and not ties("-1.00")  # the IAS 8 arithmetic stays (post-flip tool)


def test_every_control_account_must_have_a_recomputed_subledger_opening():
    sub = {k: v for k, v in SUBLEDGER.items() if k != "1400"}
    assert refusal(opening.opening_proof, inputs(sub=sub)) == "subledger_incomplete"


def test_a_missing_control_account_in_the_tb_counts_as_zero_and_must_tie():
    rows = [r for r in GOOD_TB if r[0] != "1410"] + [("3100", "0", "300.00")]
    rows = [r for r in rows if not (r[0] == "3100" and r[2] == "350.00")]
    proof = opening.opening_proof(inputs(rows))
    assert proof["ok"] and not proof["control_accounts"]["1410"]["ties"]  # 1410 subledger is 50, TB has none: reported


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
        "source_invoice_id": ref if rule == "T-ROUND-2DP" and scope == "header" else None,
    }


LAST: dict = {}


def bridge_inputs(items, legacy, refs=None, rounding="6900"):
    items, itemised, import_sha = bind_rounding(items, SHA)
    LAST.update(itemised=itemised, sha=import_sha)
    raw = {
        "version": 1,
        "snapshot_sha256": SHA,
        "rounding_account_code": rounding,
        "items": items,
    }
    universe = frozenset(refs if refs is not None else {i["ref"] for i in items})
    # the legacy side and the universe are recomputed from the snapshot, never read from the file
    return bridge.with_snapshot(bridge.parse_inputs(raw, SHA), {k: D(v) for k, v in legacy.items()}, universe)


def compute(inp, **kw):
    return bridge.compute(
        inp,
        opening_net=kw.get("opening_net"),
        ias8=kw.get("ias8", []),
        opening_tb_sha=None,
        ias8_sha=None,
        import_itemised=kw.get("import_itemised", LAST.get("itemised")),
        import_sha=kw.get("import_sha", LAST.get("sha")),
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
    # the GL moves by (subledger - legacy) so that GL(2000) equals the subledger; the offset is the rounding account
    assert row(body, "2000", "2026-01")["rounding"] == "-0.001000"
    assert row(body, "6900", "2026-01")["rounding"] == "0.001000"
    ap = row(body, "2000", "2026-01")
    assert ap["transform_delta"] == "0.000000"  # a subledger-only transform does not move the GL v2 side
    assert ap["subledger_delta"] == "-0.001000" and D(ap["v2_planned"]) - D(ap["legacy"]) == D(ap["subledger_delta"])
    assert body["ready"]  # nothing queued, no gap


def bind_keep(items, itemised):
    """Keep the original (honest) fingerprints on items whose amounts were then altered."""
    fp = {line["invoice_id"]: line["fingerprint"] for line in itemised["bridge_lines"]}
    return [{**i, "evidence_sha256": fp[i["ref"]]} if i["rule"] == "T-ROUND-2DP" else i for i in items]


def test_a_forged_rounding_item_is_refused():
    inp = bridge_inputs(ROUND_ITEMS, ROUND_LEGACY)
    itemised, sha = dict(LAST["itemised"]), LAST["sha"]

    def with_items(items):
        raw = {"version": 1, "snapshot_sha256": SHA, "rounding_account_code": "6900", "items": items}
        return bridge.with_snapshot(bridge.parse_inputs(raw, SHA), inp.legacy, inp.universe)

    # an item whose fingerprint the #68 file does not carry
    forged = [{**i, "evidence_sha256": "e" * 64} if i["item_id"] == "r1" else i for i in ROUND_ITEMS]
    assert refusal(compute, with_items(forged), import_itemised=itemised, import_sha=sha) == "rounding_item_forged"
    # an amount altered after binding (honest fingerprint, changed amount)
    altered = [{**i, "v2_amount": "-100.009"} if i["item_id"] == "r1" else i for i in inp_items(inp)]
    assert refusal(compute, with_items(altered), import_itemised=itemised, import_sha=sha) == "rounding_item_forged"
    # an item pointing at another invoice
    moved = [{**i, "source_invoice_id": "ZZ"} if i["item_id"] == "r1" else i for i in inp_items(inp)]
    assert refusal(compute, with_items(moved), import_itemised=itemised, import_sha=sha) == "rounding_item_forged"
    # the itemised file is not the one the receipt names; none at all; another snapshot
    assert refusal(compute, inp, import_itemised=itemised, import_sha="0" * 64) == "import_itemised_digest_mismatch"
    assert refusal(compute, inp, import_itemised=None, import_sha=None) == "rounding_binding_required"
    other = {**itemised, "binding": {"snapshot_sha256": "9" * 64}}
    assert (
        refusal(compute, inp, import_itemised=other, import_sha=bridge.canonical_sha(other))
        == "import_itemised_snapshot_mismatch"
    )


def inp_items(inp):
    return [
        i.as_dict() | {"legacy_amount": i.as_dict()["legacy_amount"], "evidence_sha256": i.evidence_sha256}
        for i in inp.items
    ]


def test_an_oversize_rounding_delta_is_refused():
    big = [item("r1", "transform", "A", "2000", "2026-01", "-100.00", "-100.01", rule="T-ROUND-2DP", scope="header")]
    assert refusal(compute, bridge_inputs(big, {("2000", "2026-01"): "-100.00"})) == "rounding_delta_oversize"


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


def test_the_class_list_is_the_plan_inventory_ids_and_carries_the_plan_sha():
    import hashlib
    import os
    import re

    from erp_v2.migration_ledger.classes import (
        AS_IS_CLASSES,
        CLASSES,
        INVENTORY_CLASSES,
        INVENTORY_PLAN_SHA256,
        PLAN_SHA256,
    )

    assert PLAN_SHA256 == "a28adf06752c43cd69481830c1b0e8d02a36f94a3bbe3c2ce35443d46b28de54"  # AS-IS-MIGRATION-REPLAN
    assert AS_IS_CLASSES == ("E35", "E36a", "E36b", "E37", "E38", "E39")
    assert CLASSES == (*INVENTORY_CLASSES, *AS_IS_CLASSES) and len(CLASSES) == len(set(CLASSES)) == 52
    assert (
        len(INVENTORY_CLASSES) == 46
        and INVENTORY_PLAN_SHA256 == "7160002d479d502df384ea98c6b150c9ceadf2f0b3a5b36e00a71ad8b63be608"
    )
    plan = Path(
        os.environ.get(
            "EXCEPTIONS_ZERO_PLAN",
            Path(__file__).resolve().parents[2]
            / ".cutover-private/erp-v2-step4-workflows-20261114/EXCEPTIONS-ZERO-PLAN.md",
        )
    )
    if not plan.exists():
        pytest.skip("the private plan file is not available (CI); the constant is pinned by its sha above")
    data = plan.read_bytes()
    assert hashlib.sha256(data).hexdigest() == INVENTORY_PLAN_SHA256, "the plan changed: re-derive the class list"
    ids = re.findall(r"^\| (E\d+[a-z]?) ", data.decode(), flags=re.M)
    assert tuple(ids) == INVENTORY_CLASSES


def test_the_gate_control_lists_are_code_constants():
    from erp_v2 import ledger_roles

    assert ledger_roles.GATE_TIE_CODES == ("1400", "1410", "2000", "2040", "2045")
    assert ledger_roles.REPORT_ZERO_CODES == ("2030",) and ledger_roles.REPORT_WHT_CODES == ("2110",)
    assert not hasattr(ledger_roles, "GATE_ZERO_CODES") and not hasattr(ledger_roles, "GATE_WHT_CODES")
    from erp_v2.migration_ledger import ledger_check

    assert ledger_check.GATE_PROFILE == "as_is_v1"
    assert ledger_check.control_lists() == {
        "tie": ["1400", "1410", "2000", "2040", "2045"],
        "report_zero": ["2030"],
        "report_wht": ["2110"],
    }


# --- E15 sub-kobo (T-SUBKOBO, E15-AND-SIDEFILE-RULING) -------------------------------------------------------------


def subkobo_file(*, net_debit="100.000000", net_credit="99.999999", day="2026-04-18", cutoff="2025-12-31", **over):
    import hashlib
    import json

    lines = [
        {"ledger_line_id": "l1", "journal_line_id": "j1", "account_id": "a1", "account_code": "5000",
         "posting_date": day, "debit": net_debit, "credit": "0.000000", "original_currency": None},
        {"ledger_line_id": "l2", "journal_line_id": "j2", "account_id": "a2", "account_code": "1000",
         "posting_date": day, "debit": "0.000000", "credit": net_credit, "original_currency": None},
    ]  # fmt: skip
    net = D(net_debit) - D(net_credit)
    journal = {
        "class": "E15", "reason": "unbalanced_journal_unit", "journal": "J-E15", "type": "UNBALANCED",
        "legacy_status": "POSTED", "journal_date": day, "source_module": "GL", "source_document_type": "x",
        "source_document_id": "y", "currency": "NGN", "lines": lines,
        "evidence_sha256": hashlib.sha256(json.dumps(lines, sort_keys=True).encode()).hexdigest(),
        "post_net": f"{net:f}", "net": f"{net:f}",
    }  # fmt: skip
    journal.update(over)
    return {"history_cutoff": cutoff, "class": "E15", "reason": "unbalanced_journal_unit", "journals": [journal]}


def test_the_subkobo_side_file_is_parsed_strictly_and_the_net_is_recomputed():
    cutoff, journals = subkobo.parse_side_file(subkobo_file())
    j = journals[0]
    assert cutoff.isoformat() == "2025-12-31" and (j.journal_id, j.period) == ("J-E15", "2026-04")
    assert j.net == D("0.000001") and j.residue == D("-0.000001")
    assert j.cells == (("1000", "2026-04", D("-99.999999")), ("5000", "2026-04", D("100.000000")))
    assert refusal(subkobo.parse_side_file, subkobo_file(net="0.5")) == "subkobo_net_differs_from_lines"
    assert refusal(subkobo.parse_side_file, subkobo_file(evidence_sha256="0" * 64)) == "evidence_sha_mismatch"
    assert refusal(subkobo.parse_side_file, subkobo_file(day="2025-12-31")) == "subkobo_splits_cutoff"
    bad = subkobo_file()
    bad["extra"] = 1
    assert refusal(subkobo.parse_side_file, bad) == "invalid_subkobo_side_file"
    balanced = subkobo_file(net_credit="100.000000")
    assert refusal(subkobo.parse_side_file, balanced) == "subkobo_residue_out_of_range"
    wide = subkobo_file(net_credit="99.000000")  # net 1.00: out of range (the file's own net would also differ)
    assert refusal(subkobo.parse_side_file, wide) in {"subkobo_residue_out_of_range", "subkobo_net_differs_from_lines"}


def test_the_subkobo_side_file_is_bound_to_the_import_receipt_and_the_opening_date():
    import hashlib
    import json

    raw = subkobo_file()
    sha = hashlib.sha256(json.dumps(raw).encode()).hexdigest()
    receipt = {"kind": "execute", "dry_run": False, "complete": True, "history_cutoff": "2025-12-31",
               "history_cutoff_census": {"subkobo": {"side_file_sha256": sha}}}  # fmt: skip
    d = date(2025, 12, 31)
    assert len(subkobo.bind_and_parse(raw, sha, receipt, d)) == 1
    assert refusal(subkobo.bind_and_parse, raw, "0" * 64, receipt, d) == "subkobo_side_file_not_bound_to_receipt"
    assert refusal(subkobo.bind_and_parse, raw, sha, {**receipt, "history_cutoff_census": {}}, d) == (
        "subkobo_side_file_not_bound_to_receipt"
    )
    assert refusal(subkobo.bind_and_parse, raw, sha, receipt, date(2025, 12, 30)) == "subkobo_cutoff_mismatch"


def subkobo_bridge(items_extra=(), legacy_rounding="0"):
    _c, journals = subkobo.parse_side_file(subkobo_file())
    derived = [i.as_dict() for i in subkobo.bridge_items(journals, "6900")]
    items = [*derived, *items_extra]
    legacy = {("5000", "2026-04"): "100.000000", ("1000", "2026-04"): "-99.999999"}
    inp = bridge_inputs(items, legacy, refs={"J-E15"})
    return inp, journals


def test_t_subkobo_items_cover_the_journal_and_tie_the_rounding_cell_to_the_residue():
    inp, journals = subkobo_bridge()
    body = bridge.compute(inp, opening_net=None, ias8=[], opening_tb_sha=None, ias8_sha=None, subkobo=journals)[0]
    assert body["partition"]["ok"] and body["partition_gap_abs_total"] == "0.000000" and body["ready"]
    cell = row(body, "6900", "2026-04")
    assert cell["v2_planned"] == "-0.000001" and cell["difference"] == "-0.000001" and cell["legacy"] == "0.000000"
    assert row(body, "5000", "2026-04")["difference"] == "0.000000"  # v2 equals legacy on every touched account
    assert body["subkobo"]["residue_by_period"] == {"2026-04": "-0.000001"}


def test_t_subkobo_items_must_equal_the_side_file_derivation():
    inp, journals = subkobo_bridge()
    run = lambda i, j: bridge.compute(i, opening_net=None, ias8=[], opening_tb_sha=None, ias8_sha=None, subkobo=j)  # noqa: E731
    assert refusal(run, inp, ()) == "subkobo_items_forged"  # items without a side file
    forged = [dict(i) for i in (x.as_dict() for x in inp.items)]
    forged[-1]["v2_amount"] = "-0.000002"
    inp2 = bridge_inputs(forged, {("5000", "2026-04"): "100.000000", ("1000", "2026-04"): "-99.999999"}, refs={"J-E15"})
    assert refusal(run, inp2, journals) == "subkobo_items_forged"
    assert refusal(subkobo.bridge_items, journals, None) == "rounding_account_required"
