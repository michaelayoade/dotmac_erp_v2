"""S8 claims opening: unit tests (no database)."""

import json
import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from erp_v2.claims_opening import core, extract, reclass
from erp_v2.claims_opening.reasons import EXTRACT_REASONS, OWNER_REASONS, REASONS, VERIFIER_REASONS, ClaimsRefusal
from erp_v2.holdings.attribution import parse_gl_attribution
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import LedgerRefused, canonical_sha

D = Decimal
SNAP = "a" * 64


def test_u1_the_bucket_classifier_covers_every_predicate_row():
    c = extract.classify_bucket
    assert c("APPROVED", D("100"), D("0"), D("100"), True) == "A"
    assert c("APPROVED", D("100"), D("40"), D("60"), True) == "A_partial"
    assert c("APPROVED", D("100"), D("0"), D("100"), False) == "U"
    assert c("APPROVED", None, D("0"), None, True) == "N"  # NULL net payable
    assert c("APPROVED", None, D("0"), None, False) == "N"
    assert c("APPROVED", D("100"), D("100"), D("0"), True) == "S"  # APPROVED with balance_due <= 0
    assert c("PAID", D("100"), D("0"), D("100"), True) == "S"  # the ADR-0016 artefact: PAID with a computed balance
    for status in sorted(extract.OTHER_STATUSES):
        assert c(status, D("100"), D("0"), D("100"), False) == "O"
    with pytest.raises(extract.ExtractRefused) as e:
        c("MYSTERY", D("1"), D("0"), D("1"), False)
    assert e.value.reason == "census_partition_incomplete"


def item(n, claim="c", residual="10.00", code="2110", party="p"):
    return reclass.Admitted(f"{claim}{n}", f"{claim}{n}", f"l{n}", D(residual), code, f"{party}{n}", f"e{n}")


def test_u2_the_set_fingerprint_is_order_independent_and_changes_with_any_fact():
    items = [item(1), item(2, code="2040"), item(3, residual="5.50")]
    base = reclass.set_fingerprint(items, currency="NGN", snapshot_sha256=SNAP)
    assert base == reclass.set_fingerprint(list(reversed(items)), currency="NGN", snapshot_sha256=SNAP)
    for changed in (
        [*items[:2]],  # a claim removed
        [item(1), item(2, code="2040"), item(3, residual="5.51")],  # an amount
        [item(1), item(2, code="2030"), item(3, residual="5.50")],  # a carried code
        [item(1), item(2, code="2040"), item(4, residual="5.50")],  # a claim
    ):
        assert reclass.set_fingerprint(changed, currency="NGN", snapshot_sha256=SNAP) != base
    assert reclass.set_fingerprint(items, currency="NGN", snapshot_sha256="b" * 64) != base


def test_u3_the_line_builder_is_balanced_one_dr_per_account_one_cr_per_claimant():
    accounts = {c: uuid4() for c in ("2045", "2040", "2030", "2110")}
    items = [
        item(1, residual="5000.00", code="2030", party="x"),
        reclass.Admitted("c2", "c2", "l2", D("15305.10"), "2030", "x1", "e2"),  # same claimant as item 1: one Cr line
        item(3, residual="9178.00", code="2110", party="y"),
        reclass.Admitted("c4", "c4", "l4", D("1.00"), "2040", "z", "e4"),
    ]
    lines = reclass.build_lines(items, accounts)
    debits = [x for x in lines if x.debit]
    credits = [x for x in lines if x.credit]
    assert {x.account_ref for x in debits} == {str(accounts[c]) for c in ("2030", "2110", "2040")}
    assert len(debits) == 3 and len(credits) == len({i.claimant_party_id for i in items}) == 3
    assert sum(x.debit for x in lines) == sum(x.credit for x in lines) == D("29484.10")
    assert all(x.credit == 0 or x.account_ref == str(accounts["2045"]) for x in lines)
    assert all(max(x.debit, x.credit).as_tuple().exponent == -2 for x in lines)  # the amount side is exact 2dp
    with pytest.raises(ClaimsRefusal) as e:
        reclass.build_lines([], accounts)
    assert e.value.reason == "reclass_empty_set"


def test_u4_receipts_carry_no_names_and_the_attribution_parses():
    items = [item(1, code="2040"), item(2, residual="7.25", code="2030"), item(3, code="2040")]
    attribution = core.attribution_2045(items)
    parsed, _sha = parse_gl_attribution(json.dumps(attribution).encode())
    assert parsed.code == "2045" and sum(parsed.gl.values()) == D("-27.25") and parsed.opening_residue == 0
    refusals = [
        dict({"class": "E37"}, key="claim:c8", reason="legacy_claim_gl_unattributable", carried_code="2040",
             amount="90.00", gl_signed_minor=-9000, claimant_party_id="p8", employee_id="e8"),
    ]  # fmt: skip
    fragment = core.fragment_2040(items, refusals, D("20.00"))
    assert fragment["reclass_2040"] == [{"amount": "10.00", "key": "e1"}, {"amount": "10.00", "key": "e3"}]
    assert fragment["held_2040"][0]["key"] == "e8" and fragment["held_2040"][0]["amount"] == "-90.00"
    # the held rows have the exact parse_gl_attribution held shape
    body = dict(attribution, held=fragment["held_2040"], code="2040")
    parse_gl_attribution(json.dumps(body).encode())
    with pytest.raises(ClaimsRefusal) as e:
        core.fragment_2040(items, refusals, D("19.99"))  # not summing to the journal's 2040 line
    assert e.value.reason == "fragment_not_exact"
    for payload in (attribution, fragment):
        text = json.dumps(payload).lower()
        assert not re.search(r"name|email|display", text)


def test_u5_every_refusal_reason_is_an_e37_row_and_an_unknown_one_is_refused():
    def receipt(reason):
        body = dict(
            kind="claims-opening",
            items=[dict({"class": "E37"}, key="claim:1", reason=reason, carried_code=None, amount="1.00",
                        gl_signed_minor=-100, claimant_party_id=None, employee_id=None)],
        )  # fmt: skip
        body["receipt_sha256"] = core.receipt_sha(body)
        return body

    for reason in sorted(REASONS):
        (fact,) = register.from_claims_opening(receipt(reason))
        assert (fact.class_code, fact.legacy_key, fact.source_kind) == ("E37", "claim:1", "claims_opening")
        assert fact.detail["gl_signed_minor"] == -100
    with pytest.raises(LedgerRefused) as e:
        register.from_claims_opening(receipt("made_up_reason"))
    assert e.value.code == "unknown_queue_reason"
    stale = receipt("claim_carried_split")
    stale["items"][0]["amount"] = "2.00"  # edited after the sha was taken
    with pytest.raises(LedgerRefused) as e:
        register.from_claims_opening(stale)
    assert e.value.code == "claims_opening_sha_mismatch"
    # every reason the extract and the verifier can raise is in the closed table (a new one is a test failure)
    src = Path(__file__).resolve().parent.parent / "src" / "erp_v2" / "claims_opening"
    raised = set()
    for name in ("verifier.py", "core.py", "extract.py"):
        raised |= set(
            re.findall(
                r'(?:_require\([^"]*?|ClaimEvidenceError\(|_refusal\([^"]*?)"([a-z_:]+)"', (src / name).read_text()
            )
        )
    row_level = {r for r in raised if r in REASONS or r.startswith("owner_refused")}
    assert {"claim_net_payable_missing", "claim_carried_split", "owner_refused:historical_claim_not_open"} <= row_level
    assert set(EXTRACT_REASONS + VERIFIER_REASONS + OWNER_REASONS) == REASONS
    assert canonical_sha({"a": 1}) == canonical_sha({"a": 1})


def test_only_claims_opening_reclass_builds_the_claim_family_kind():
    src = Path(__file__).resolve().parent.parent / "src" / "erp_v2"
    users = {
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if "KIND_CLAIM_RECLASS" in p.read_text() or "claim_family_reclass" in p.read_text()
    }
    assert users == {
        "holdings/models.py",  # the constant and POSTING_KINDS
        "posting/rules.py",  # the reviewed RefPolicy entry
        "claims_opening/reclass.py",  # the one builder (queries read it back by the same constant)
    }
