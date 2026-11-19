# ruff: noqa: F811  (the imported `db` fixture is used by name)
"""B-3 transform manifest through the verifier gate (unpaid admission; SQLite fixture of the historical suite).

The verifier RE-DERIVES each invoice's transform from the snapshot with the declared fallback account and requires
manifest == re-derived == destination. Combinations per the ruling: transform alone, waiver alone, transform plus
waiver, a transform that does not cover the failure, and a forged manifest (refused).
"""

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_payables_history import EXP, INVOICE, T, db, evidence  # noqa: F401
from test_reviewed_admission import add_review, break_line_totals

from erp_v2.importer.payables_history import HistoricalEvidenceError
from erp_v2.importer.transforms import T_EXP_FALLBACK, T_ROUND_2DP, canonical_digest
from erp_v2.legacy_ap.transform_manifest import InvoiceTransform, TransformManifest, TransformManifestError

FALLBACK = str(UUID(int=0x5999))  # the declared fallback expense account
FORGED = str(UUID(int=0x5100))
SNAPSHOT = "d" * 64  # the snapshot of the historical-evidence fixture manifest


def line_id(db):
    table = db[4][True, "supplier_invoice_line"]
    return str(UUID(str(db[0].execute(table.select()).mappings().one()["line_id"])))


def null_expense(db):
    """Legacy left the line without an expense account; the importer landed it on the declared fallback."""
    db[0].exec_driver_sql("PRAGMA query_only=0")
    db[0].execute(db[4][True, "supplier_invoice_line"].update().values(expense_account_id=None))
    db[1].execute(db[4][False, "supplier_invoice_lines"].update().values(posting_account_ref=FALLBACK))


def body(db, *, amount="100", account=FALLBACK, rules=(T_EXP_FALLBACK,), invoice_id=INVOICE, **binding):
    landed = {
        "rules": sorted(rules),
        "header": {},
        "lines": {
            line_id(db): {
                "posting_account_ref": account,
                "tax_account_ref": None,
                "line_amount": amount,
                "tax_amount": "0",
            }
        },
        "item_lines": [],
    }
    return {
        "binding": {"snapshot_sha256": SNAPSHOT, **binding},
        "bridge_lines": [],
        "queue": [],
        "changed_headers": [],
        "landed": {invoice_id: landed},
        "fallback_account": FALLBACK,
    }


def verified(db, data, *, fallback=FALLBACK):
    db[0].exec_driver_sql("PRAGMA query_only=0")
    if not db[0].exec_driver_sql("SELECT name FROM sqlite_master WHERE name='account'").first():
        db[0].exec_driver_sql(
            "CREATE TABLE account (account_id TEXT, deferral_pair_account_id TEXT, account_code TEXT, "
            "is_active INTEGER, is_posting_allowed INTEGER)"
        )
        db[0].exec_driver_sql("INSERT INTO account VALUES (?, NULL, '5000', 1, 1)", (EXP,))
    db[0].exec_driver_sql("PRAGMA query_only=1")
    verifier = replace(
        db[3], transform_manifest=TransformManifest.from_itemised(data), fallback_expense_account=fallback
    )
    return verifier.evidence(scope=SimpleNamespace(tenant_id=UUID(T)), invoice_id=UUID(INVOICE), destination=db[2])


def test_without_a_manifest_a_transformed_line_is_refused(db):
    null_expense(db)
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_posting_account_ref"):
        evidence(db)


def test_transform_alone_admits_without_a_waiver_and_changes_the_fingerprint(db):
    null_expense(db)
    item = verified(db, body(db))
    assert item.total_amount == Decimal("100")
    # no review row exists: a transform needed no waiver and tripped no waiver_not_needed
    assert item.fingerprint().startswith("cv1:")


def test_waiver_alone_still_works_and_does_not_need_a_manifest(db):
    break_line_totals(db)
    add_review(db, waived=("line_totals",))
    assert evidence(db).total_amount == Decimal("100")


def test_transform_plus_waiver_admits_and_both_are_part_of_the_fingerprint(db):
    break_line_totals(db)  # source and destination line = 99, header 100: a line_totals failure
    null_expense(db)
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_posting_account_ref"):
        evidence(db)  # a waiver cannot explain the moved account
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_totals"):
        verified(db, body(db, amount="99"))  # a transform cannot explain the totals
    add_review(db, waived=("line_totals",))
    both = verified(db, body(db, amount="99"))
    assert both.total_amount == Decimal("100")
    review = db[3].review(scope=SimpleNamespace(tenant_id=UUID(T)), invoice_id=UUID(INVOICE), destination=db[2])
    assert review.waived == ("line_totals",)


def test_a_forged_manifest_over_an_edited_destination_line_is_refused(db):
    """The destination line was edited and a manifest names the edit: re-derivation says no rule produces it."""
    db[1].execute(db[4][False, "supplier_invoice_lines"].update().values(posting_account_ref=FORGED))
    with pytest.raises(HistoricalEvidenceError, match="transform_manifest_mismatch"):
        verified(db, body(db, account=FORGED))


def test_a_manifest_that_claims_a_rule_the_snapshot_does_not_support_is_refused(db):
    with pytest.raises(HistoricalEvidenceError, match="transform_manifest_mismatch"):
        verified(db, body(db, rules=(T_ROUND_2DP,), account=FALLBACK))  # nothing in the source to transform


def test_manifest_bindings_must_match_the_verifier(db):
    null_expense(db)
    other_snapshot = body(db)
    other_snapshot["binding"]["snapshot_sha256"] = "e" * 64
    with pytest.raises(HistoricalEvidenceError, match="transform_manifest_snapshot"):
        verified(db, other_snapshot)
    with pytest.raises(HistoricalEvidenceError, match="transform_fallback_account"):
        verified(db, body(db), fallback=FORGED)  # the declared fallback is not the one the manifest was built with
    with pytest.raises(HistoricalEvidenceError, match="transform_fallback_account"):
        verified(db, body(db), fallback=None)


def test_the_manifest_file_must_hash_to_the_receipts_itemised_sha256(db, tmp_path):
    null_expense(db)
    data = body(db)
    path = tmp_path / "r.json.transforms.json"
    path.write_text(json.dumps(data, indent=1, sort_keys=True))
    good = canonical_digest(data)
    assert TransformManifest.load(path, good).sha256 == good
    with pytest.raises(TransformManifestError, match="transform_manifest_digest_mismatch"):
        TransformManifest.load(path, "0" * 64)
    with pytest.raises(TransformManifestError, match="transform_manifest_receipt_sha256_required"):
        TransformManifest.load(path, None)
    data["landed"][INVOICE]["lines"][line_id(db)]["posting_account_ref"] = FORGED  # edited after the import
    path.write_text(json.dumps(data))
    with pytest.raises(TransformManifestError, match="transform_manifest_digest_mismatch"):
        TransformManifest.load(path, good)


def test_round_2dp_maps_to_the_scale_check_and_nothing_else():
    assert InvoiceTransform(frozenset({T_ROUND_2DP})).waivable_checks() == {"source_amount_scale": T_ROUND_2DP}
    assert InvoiceTransform(frozenset({T_EXP_FALLBACK})).waivable_checks() == {}


def test_a_malformed_manifest_is_refused():
    for data in ({}, {"landed": []}, {"landed": {}}, {"landed": {}, "binding": {}, "fallback_account": "x"}):
        with pytest.raises(TransformManifestError):
            TransformManifest.from_itemised(data)
