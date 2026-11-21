"""GL attribution receipts for 2000 / 2040 / 1410: the pure core, the register adapter and the holdings tightening.

Units u1-u6 of GL-ATTRIBUTION-RECEIPTS-SPEC section 7, plus K2 (zero units), the closed AP admission map and the
manifest binding in the holdings run-spec. No database.
"""

import hashlib
import json
from decimal import Decimal
from uuid import uuid4

import pytest

from erp_v2.holdings import cli as holdings_cli
from erp_v2.holdings import plan as planning
from erp_v2.holdings.attribution import Party, parse_gl_attribution
from erp_v2.holdings.errors import HoldingRefused
from erp_v2.migration_ledger import attribution_rules as rules
from erp_v2.migration_ledger import gl_attribution as core
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import LedgerRefused

D = Decimal
E1, E2, E3 = Party("employee", "E1"), Party("employee", "E2"), Party("employee", "E3")
S_A, S_B = Party("supplier", "A"), Party("supplier", "B")


def line(journal, net, *, cut=None, t="X", lines=1):
    net = D(net)
    return core.LegacyLine(journal, t, net, net if cut is None else D(cut), lines)


def owners(**per):
    return {j: {p: frozenset({"R"}) for p in ps} for j, ps in per.items()}


def build(legacy, own, *, code="2040", v2=None, gl=None, zero=frozenset(), held=(), fragment=None):
    v2 = (
        v2
        if v2 is not None
        else [core.V2Journal("v2-legacy", "legacy", sum((ln.net - ln.net_cut for ln in legacy), D(0)))]
    )
    opening = sum((ln.net_cut for ln in legacy), D(0))
    v2 = [core.V2Journal("v2-opening", "opening", opening), *v2]
    total = gl if gl is not None else sum((j.net for j in v2), D(0))
    return core.build_code_receipt(
        core.CodeInputs(code, "NGN", tuple(legacy), own, zero, total, tuple(v2), tuple(held), fragment)
    )


# --- u1: owner resolution outcomes ----------------------------------------------------------------------------------


def test_u1_one_none_and_many_owners_stay_unsplit_and_shared_is_marked():
    r = build(
        [line("j1", "-10.00", cut="0"), line("j2", "-5.00", cut="0"), line("j3", "-7.00", cut="0")],
        owners(j1=[E1], j3=[E1, E2]),
    )
    assert r.body["parties"] == [{"kind": "employee", "key": "E1", "gl": "-10.00"}]
    assert r.body["unattributed"] == [
        {"journal_id": "j2", "amount": "-5.00", "shared": False},
        {"journal_id": "j3", "amount": "-7.00", "shared": True},
    ]
    assert r.summary["shared"] == 1
    assert {row["journals_by_outcome"].get("shared") for row in r.by_source_type} == {1}


def test_u1_the_rule_tables_pin_every_owner_rule_per_code_and_the_p_attr_receipt():
    assert rules.P_ATTR_RECEIPT_SHA256.startswith("0daa2c13")
    assert set(rules.OWNER_SQL) == {"2000", "2040"}  # 1410 has no document journal column: all unattributed
    assert rules.PARTY_KIND == {"2000": "supplier", "2040": "employee", "1410": "employee"}
    # a rule id in the SQL is a rule id the manifest knows, and no free-text column is ever a join key
    for code, sql in rules.OWNER_SQL.items():
        for rule in rules.OWNER_RULES[code]:
            assert f"'{rule}'" in sql or rule in (
                "L:EXPENSE_CLAIM",
                "L:EXPENSE_REIMBURSEMENT",
                "L:RECLASS",
                "J:claim_reversal",
            )
        assert "description" not in sql and "reference" not in sql.replace("reimbursement", "")


# --- u2: signs and exact 2-dp amounts -----------------------------------------------------------------------------


def test_u2_liabilities_are_negative_and_an_inexact_amount_refuses_never_rounds():
    r = build([line("j1", "-10.50", cut="0")], owners(j1=[S_A]), code="2000")
    assert r.body["parties"][0]["gl"] == "-10.50" and r.body["code"] == "2000"
    with pytest.raises(LedgerRefused) as e:
        build([line("j1", "-10.505", cut="0")], owners(j1=[S_A]), code="2000")
    assert e.value.code == "gl_amount_not_exact:j1"
    assert core.fmt2(D("-3")) == "-3.00"


# --- u3: invariant, opening residue, legacy_import_differs ---------------------------------------------------------


def test_u3_the_invariant_the_opening_residue_and_legacy_import_differs():
    legacy = [line("pre", "-100.00", cut="-100.00"), line("post", "-20.00", cut="0")]
    ok = build(legacy, owners(pre=[E1], post=[E1]))
    assert ok.body["opening_residue"] == "0.00" and ok.body["parties"][0]["gl"] == "-120.00"
    # the opening journal in v2 is 0.30 more than the legacy cumulative balance: its own residue, never a party plug
    opening = [core.V2Journal("v2-legacy", "legacy", D("-20.00"))]
    legacy2 = [line("pre", "-100.00", cut="-100.00"), line("post", "-20.00", cut="0")]
    r = core.build_code_receipt(
        core.CodeInputs(
            "2040", "NGN", tuple(legacy2), owners(pre=[E1], post=[E1]), frozenset(), D("-120.30"),
            (core.V2Journal("v2-opening", "opening", D("-100.30")), *opening),
        )
    )  # fmt: skip
    assert r.body["opening_residue"] == "-0.30" and r.body["parties"][0]["gl"] == "-120.00"
    # v2 carries a different FY2026 amount than the legacy ledger after the cutoff
    with pytest.raises(LedgerRefused) as e:
        build(legacy, owners(pre=[E1], post=[E1]), v2=[core.V2Journal("v2-legacy", "legacy", D("-19.00"))])
    assert e.value.code == "legacy_import_differs:2040"
    # a ledger total that the receipt does not cover
    with pytest.raises(LedgerRefused) as e:
        build(legacy, owners(pre=[E1], post=[E1]), gl=D("-121.00"))
    assert e.value.code == "attribution_not_exact:2040"


def test_u3_a_rounding_journal_gets_its_own_unattributed_row_and_native_kinds_are_not_receipt_classes():
    legacy = [line("j1", "-10.00", cut="0")]
    v2 = [core.V2Journal("v2-legacy", "legacy", D("-10.00")), core.V2Journal("rnd", "rounding", D("0.01"))]
    r = build(legacy, owners(j1=[E1]), v2=v2)
    assert {"journal_id": "rnd", "amount": "0.01", "shared": False} in r.body["unattributed"]


# --- u4: output parses with parse_gl_attribution and round-trips; the manifest lists every sha -----------------------


def test_u4_the_output_parses_round_trips_and_the_manifest_lists_every_sha():
    legacy = [line("j1", "-10.00", cut="0"), line("j2", "-4.00", cut="0")]
    r = build(legacy, owners(j1=[E1]))
    att, sha = parse_gl_attribution(r.raw)
    assert sha == r.sha256 and att.code == "2040" and att.gl == {E1: D("-10.00")}
    assert att.unattributed[0].journal_id == "j2" and att.gl_total() == D("-14.00")
    assert core.dumps(json.loads(r.raw)) == r.raw  # round trip, byte for byte
    manifest = core.build_manifest(
        tenant_scope="a" * 64, capture_date="2026-01-20",
        sources={"legacy_snapshot": "1" * 64, "cutoff_receipt": "2" * 64, "p_attr_receipt": rules.P_ATTR_RECEIPT_SHA256,
                 "import_itemised": None, "unpaid_plan": None, "settled_plan": "3" * 64, "s8_receipt": None,
                 "s8_fragment": None},
        v2_posted_sha256="4" * 64, receipts={"2040": r}, reasons_unclassified={"x": 2},
    )  # fmt: skip
    assert manifest["codes"]["2040"]["receipt_sha256"] == r.sha256
    assert manifest["p_attr_receipt_sha256"] == rules.P_ATTR_RECEIPT_SHA256 and manifest["reasons_unclassified"] == {
        "x": 2
    }
    assert (
        manifest["sources"]["settled_plan"] == "3" * 64 and manifest["by_source_type"][0]["source_document_type"] == "X"
    )
    body, sha = core.parse_manifest(core.dumps(manifest))
    assert body["v2_posted_sha256"] == "4" * 64 and len(sha) == 64
    with pytest.raises(LedgerRefused):
        core.parse_manifest(b'{"kind": "other", "version": 1}')


# --- K2: zero units ------------------------------------------------------------------------------------------------


def test_k2_exactly_cancelling_pairs_are_zero_units_and_every_other_case_is_not_paired():
    rows = [
        core.K2Row("r1", "s1", True, True),  # cancels: paired
        core.K2Row("r2", "s2", True, True),  # differs by 0.01: not paired
        core.K2Row("r3", "s3", True, True),  # s3 referenced twice: not paired
        core.K2Row("r4", "s3", True, True),
        core.K2Row("r5", "s5", True, False),  # source not posted
        core.K2Row("r6", None, True, False),  # no such source
        core.K2Row("r7", "s7", False, True),  # reclass not posted
    ]
    nets = {"r1": D("-10"), "s1": D("10"), "r2": D("-10.01"), "s2": D("10"), "r3": D("-1"), "r4": D("-1"), "s3": D("2")}
    units, on_code = core.zero_units(rows, nets)
    assert units == {"r1", "s1"} and on_code == 1


def test_k2_a_zero_unit_is_left_out_of_the_receipt_and_the_invariant_still_holds():
    legacy = [line("s", "-40.00", cut="0"), line("r", "40.00", cut="0"), line("x", "-3.00", cut="0")]
    r = build(legacy, owners(s=[E1], x=[E2]), zero=frozenset({"s", "r"}))
    assert r.body["parties"] == [{"kind": "employee", "key": "E2", "gl": "-3.00"}]
    assert r.body["unattributed"] == [] and r.summary["zero_unit_journals"] == 2


# --- u5: held derivation equals the register adapter's facts -------------------------------------------------------


def itemised_body(*invoices, reason="total_mismatch_after_transform"):
    return {"queue": [{"invoice_id": i, "reasons": [reason], "lands": False, "legacy_total": "1"} for i in invoices]}


def test_u5_held_rows_use_the_register_adapters_class_and_key_and_skip_unposted_invoices():
    facts = register.from_import_itemised(itemised_body("c1", "c2"))
    plan = {
        "refusals": [{"invoice_id": "b1", "reason": "source_journal_status"}, {"invoice_id": "b2", "reason": "weird"}]
    }
    classified, unclassified = register.classify_ap_admission(plan)
    adapter_facts = register.from_ap_admission(plan)
    assert [(f.class_code, f.legacy_key) for f in adapter_facts] == [("E02", "invoice:b1")]
    assert classified == [("E02", "invoice:b1", "source_journal_status", None)]
    assert unclassified == [("invoice:b2", "weird")]  # not explained: it falls into the supplier's partygap
    invoices = {
        "c1": core.InvoiceFact("C", D("30.00"), True),
        "c2": core.InvoiceFact("C", D("5.00"), False),  # legacy-unposted: no GL, no held row
        "b1": core.InvoiceFact("B", D("-8.00"), True),  # a credit note: residual negative, held positive
        "b2": core.InvoiceFact("B", D("2.00"), True),
    }
    rows, counts = core.held_2000(
        itemised=[(f.class_code, f.legacy_key, ",".join(f.detail["reasons"])) for f in facts],
        admission=classified,
        unclassified=unclassified,
        invoices=invoices,
    )
    got = {(h.party.key, h.class_code, h.legacy_key): h.amount for h in rows}
    assert got == {("C", "E04", "invoice:c1"): D("-30.00"), ("B", "E02", "invoice:b1"): D("8.00")}
    assert counts == {"weird": 1}


def test_u5_an_invoice_in_two_sources_refuses_and_an_unknown_invoice_refuses():
    inv = {"i1": core.InvoiceFact("A", D("1.00"), True)}
    with pytest.raises(LedgerRefused) as e:
        core.held_2000(
            itemised=[("E04", "invoice:i1", "r")],
            admission=[("E02", "invoice:i1", "r", None)],
            unclassified=[],
            invoices=inv,
        )
    assert e.value.code == "held_source_overlap"
    with pytest.raises(LedgerRefused) as e:
        core.held_2000(itemised=[("E04", "invoice:zz", "r")], admission=[], unclassified=[], invoices=inv)
    assert e.value.code == "held_invoice_not_in_legacy"
    with pytest.raises(LedgerRefused) as e:  # a plan residual that differs from the legacy residual
        core.held_2000(itemised=[], admission=[("E02", "invoice:i1", "r", 5)], unclassified=[], invoices=inv)
    assert e.value.code == "held_residual_differs_from_plan"


def test_u5_the_settled_plan_shape_maps_allocation_anomalies_to_e03c_and_strips_the_evidence_prefix():
    plan = {"invoices": [
        {"invoice_id": "s1", "outcome": "refused", "reason": "legacy_evidence:allocation_sum_mismatch"},
        {"invoice_id": "s2", "outcome": "eligible", "reason": None},
        {"invoice_id": "s3", "outcome": "refused", "reason": "not_approved"},
    ]}  # fmt: skip
    facts = register.from_ap_admission(plan)
    assert [(f.class_code, f.legacy_key, f.source_kind) for f in facts] == [("E03c", "invoice:s1", "ap_admission")]
    with pytest.raises(LedgerRefused):
        register.from_ap_admission({"nothing": []})
    with_residual = register.from_ap_admission(
        {"refusals": [{"invoice_id": "u", "reason": "source_ledger_line", "residual": "12.34"}]}
    )
    assert with_residual[0].detail["gl_signed_minor"] == -1234
    assert register.SRC_AP_ADMISSION in register.RECEIPT_KINDS


# --- u6: the S8 fragment ---------------------------------------------------------------------------------------------


def fragment(reclass, held=()):
    return {
        "kind": "claims-2040-fragment", "version": 1, "currency": "NGN",
        "reclass_2040": [{"key": k, "amount": v} for k, v in reclass.items()],
        "held_2040": list(held), "held_2040_unkeyed": 0,
    }  # fmt: skip


def test_u6_an_exact_fragment_attributes_per_employee_and_a_mismatch_makes_the_journal_unattributed():
    per, held, unkeyed = core.parse_fragment(
        fragment(
            {"E1": "30.00", "E2": "20.00"},
            [
                {
                    "kind": "employee",
                    "key": "E3",
                    "class_code": "E37",
                    "reason": "r",
                    "legacy_key": "claim:9",
                    "amount": "-7.00",
                }
            ],
        )
    )
    assert per == {"E1": D("30.00"), "E2": D("20.00")} and unkeyed == 0
    assert held[0].legacy_key == "claim:9" and held[0].amount == D("-7.00") and held[0].party == E3
    v2 = lambda: [core.V2Journal("v2-opening", "opening", D("0")), core.V2Journal("s8", "claim_reclass", D("50.00"))]  # noqa: E731
    exact = core.build_code_receipt(
        core.CodeInputs("2040", "NGN", (), {}, frozenset(), D("50.00"), tuple(v2()), (), per)
    )
    assert exact.body["parties"] == [
        {"kind": "employee", "key": "E1", "gl": "30.00"},
        {"kind": "employee", "key": "E2", "gl": "20.00"},
    ]
    assert exact.flags == []
    off = {"E1": D("30.01"), "E2": D("20.00")}
    bad = core.build_code_receipt(core.CodeInputs("2040", "NGN", (), {}, frozenset(), D("50.00"), tuple(v2()), (), off))
    assert bad.body["parties"] == [] and bad.body["unattributed"] == [
        {"journal_id": "s8", "amount": "50.00", "shared": False}
    ]
    assert bad.flags == [{"journal_id": "s8", "reason": "fragment_mismatch"}]
    none = core.build_code_receipt(
        core.CodeInputs("2040", "NGN", (), {}, frozenset(), D("50.00"), tuple(v2()), (), None)
    )
    assert none.flags[0]["reason"] == "fragment_missing"
    with pytest.raises(LedgerRefused) as e:  # the S8 journal is on 2040 only
        core.build_code_receipt(core.CodeInputs("2000", "NGN", (), {}, frozenset(), D("50.00"), tuple(v2()), (), None))
    assert e.value.code == "native_posting_before_flip:2000"
    with pytest.raises(LedgerRefused):
        core.parse_fragment(
            {"kind": "claims-2040-fragment", "version": 1, "reclass_2040": [{"key": "E1"}], "held_2040": []}
        )
    with pytest.raises(LedgerRefused):
        core.parse_fragment(
            fragment(
                {},
                [
                    {
                        "kind": "employee",
                        "key": "E",
                        "class_code": "E02",
                        "reason": "r",
                        "legacy_key": "claim:1",
                        "amount": "1.00",
                    }
                ],
            )
        )


# --- the holdings tightening ---------------------------------------------------------------------------------------


TENANT = uuid4()


def spec_with(receipts, *, tenant=TENANT, capture="2026-01-20"):
    return json.dumps(
        {
            "version": 1, "tenant_id": str(tenant), "window_id": str(uuid4()), "capture_date": capture,
            "evidence_ref": "E", "params": {}, "receipts": receipts,
        }
    ).encode()  # fmt: skip


def test_the_run_spec_requires_the_manifest_when_any_gl_attribution_is_named():
    ref = {"path": "/x", "sha256": "a" * 64}
    with pytest.raises(HoldingRefused) as e:
        planning.parse_spec(spec_with({"gl_attribution": {"2000": ref}}))
    assert e.value.code == "gl_attribution_manifest_required"
    spec, _ = planning.parse_spec(spec_with({"gl_attribution": {"2000": ref}, "gl_attribution_manifest": ref}))
    assert spec.gl_attribution_manifest == planning.ReceiptRef("/x", "a" * 64)
    planning.parse_spec(spec_with({}))  # no attribution named: no manifest needed


def test_load_receipts_verifies_each_receipt_sha_is_listed_in_the_manifest(tmp_path):
    r = build([line("j1", "-10.00", cut="0")], owners(j1=[E1]))
    rec = tmp_path / "r.json"
    rec.write_bytes(r.raw)

    def manifest(shas, *, tenant=TENANT, capture="2026-01-20"):
        body = {
            "kind": "gl-attribution-manifest", "version": 1, "v2_posted_sha256": "9" * 64,
            "tenant_scope": hashlib.sha256(str(tenant).encode()).hexdigest(), "capture_date": capture,
            "codes": {f"c{i}": {"receipt_sha256": s} for i, s in enumerate(shas)},
        }  # fmt: skip
        path = tmp_path / f"m-{uuid4().hex}.json"
        path.write_bytes(json.dumps(body).encode())
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    ref = {"path": str(rec), "sha256": r.sha256}
    spec, _ = planning.parse_spec(
        spec_with({"gl_attribution": {"2040": ref}, "gl_attribution_manifest": manifest([r.sha256])})
    )
    got = holdings_cli.load_receipts(spec)
    assert got.manifest_v2_posted_sha256 == "9" * 64 and "gl_attribution_manifest" in got.shas
    spec, _ = planning.parse_spec(
        spec_with({"gl_attribution": {"2040": ref}, "gl_attribution_manifest": manifest(["f" * 64])})
    )
    with pytest.raises(HoldingRefused) as e:
        holdings_cli.load_receipts(spec)
    assert e.value.code == "gl_attribution_not_in_manifest"
    # F3: a manifest produced for another tenant or another capture date
    for other in (manifest([r.sha256], tenant=uuid4()), manifest([r.sha256], capture="2026-01-21")):
        spec, _ = planning.parse_spec(spec_with({"gl_attribution": {"2040": ref}, "gl_attribution_manifest": other}))
        with pytest.raises(HoldingRefused) as e:
            holdings_cli.load_receipts(spec)
        assert e.value.code == "attribution_manifest_other_scope"


def test_a_stale_manifest_refuses_and_a_fresh_one_passes():
    class Snap:
        ledger_sha = "1" * 64

    fresh = planning.Receipts(None, None, {}, {}, "1" * 64)
    planning.check_manifest_fresh(fresh, Snap())
    planning.check_manifest_fresh(planning.Receipts(None, None, {}, {}), Snap())  # no manifest: nothing to check
    with pytest.raises(HoldingRefused) as e:
        planning.check_manifest_fresh(planning.Receipts(None, None, {}, {}, "2" * 64), Snap())
    assert e.value.code == "attribution_manifest_stale"


def test_f4_legacy_party_keys_are_canonicalised_and_an_unparseable_key_is_left_as_is():
    from uuid import UUID

    from erp_v2.migration_ledger import attribution_cli as acli

    k = uuid4()
    assert acli._canonical(str(k).upper()) == str(k) == str(UUID(str(k)))
    assert acli._canonical("E1") == "E1"


def test_f1_sources_require_declaration_unit():
    from erp_v2.migration_ledger import attribution_cli as acli

    acli.Sources(absent=acli.SOURCE_NAMES).require()
    for kwargs, code in (
        ({"absent": ["unpaid_plan", "settled_plan", "s8"]}, "source_required:import_itemised"),
        ({"absent": ["import_itemised", "unpaid_plan", "s8"]}, "source_required:settled_plan"),
        ({"absent": ["import_itemised", "unpaid_plan", "settled_plan"]}, "source_required:s8_receipt"),
        (
            {"absent": ["import_itemised", "unpaid_plan", "settled_plan"], "s8_receipt": b"{}"},
            "source_required:s8_fragment",
        ),
    ):
        with pytest.raises(LedgerRefused) as e:
            acli.Sources(**kwargs).require()
        assert e.value.code == code
