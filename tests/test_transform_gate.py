# ruff: noqa: F811  (the imported `db` fixture is used by name)
"""B-3 transform manifest through the verifier gate (unpaid admission; SQLite fixture of the historical suite).

Four combinations per the ruling: transform alone, waiver alone, transform plus waiver, and a transform that does
not cover the failure (still refused). Transforms and waivers stay separate in the evidence.
"""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_payables_history import INVOICE, db, evidence  # noqa: F401
from test_reviewed_admission import add_review, break_line_totals

from erp_v2.importer.payables_history import HistoricalEvidenceError
from erp_v2.importer.transforms import T_EXP_LEGACY, T_ROUND_2DP
from erp_v2.legacy_ap.transform_manifest import InvoiceTransform, TransformManifest, TransformManifestError

LEGACY_GL = str(UUID(int=0x5100))  # the account the transform landed the line on


def line_id(db):
    table = db[4][True, "supplier_invoice_line"]
    return db[0].execute(table.select()).mappings().one()["line_id"]


def land_on_legacy_account(db):
    """The importer landed the line on the legacy GL account instead of the line's own expense account."""
    db[1].execute(db[4][False, "supplier_invoice_lines"].update().values(posting_account_ref=LEGACY_GL))


def manifest(db, *, amount="100", account=LEGACY_GL, rules=(T_EXP_LEGACY,), invoice_id=INVOICE):
    landed = {
        "rules": sorted(rules),
        "header": {},
        "lines": {
            str(UUID(str(line_id(db)))): {
                "posting_account_ref": account,
                "tax_account_ref": None,
                "line_amount": amount,
                "tax_amount": "0",
            }
        },
        "item_lines": [],
    }
    return TransformManifest.from_itemised({"landed": {invoice_id: landed}})


def test_without_a_manifest_a_transformed_line_is_refused(db):
    land_on_legacy_account(db)
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_posting_account_ref"):
        evidence(db)


def test_transform_alone_admits_without_a_waiver_and_is_recorded_apart_from_waivers(db):
    baseline = evidence(db).fingerprint()
    land_on_legacy_account(db)
    item = _evidence_with(db, manifest(db))
    assert item.total_amount == Decimal("100") and item.fingerprint() != baseline
    # no review row exists: the transform needed no waiver and tripped no waiver_not_needed
    verifier = replace(db[3], transform_manifest=manifest(db))
    gate = verifier.gate_for(None, UUID(INVOICE))
    assert gate.facts() == {}  # nothing waived, and nothing transformed until a check is actually fixed by one


def test_waiver_alone_still_works_and_does_not_need_a_manifest(db):
    break_line_totals(db)
    add_review(db, waived=("line_totals",))
    assert evidence(db).total_amount == Decimal("100")


def test_transform_plus_waiver_admits_and_both_are_part_of_the_fingerprint(db):
    break_line_totals(db)  # source and destination line = 99, header 100: a line_totals failure
    land_on_legacy_account(db)
    # neither alone admits: the waiver cannot explain the moved account, the transform cannot explain the totals
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_posting_account_ref"):
        evidence(db)
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_totals"):
        _evidence_with(db, manifest(db, amount="99"))
    add_review(db, waived=("line_totals",))
    both = _evidence_with(db, manifest(db, amount="99"))
    other = _evidence_with(db, manifest(db, amount="99", rules=(T_EXP_LEGACY, T_ROUND_2DP)))
    assert both.total_amount == Decimal("100") and both.fingerprint() != other.fingerprint()


def test_a_transform_does_not_cover_a_failure_it_did_not_fix(db):
    break_line_totals(db)
    land_on_legacy_account(db)
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_totals"):
        _evidence_with(db, manifest(db, amount="99"))  # no waiver: the transform explains the account, not the totals


def test_a_manifest_naming_other_values_than_the_destination_is_refused(db):
    land_on_legacy_account(db)
    other = str(UUID(int=0x5200))
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_posting_account_ref"):
        _evidence_with(db, manifest(db, account=other))
    with pytest.raises(HistoricalEvidenceError, match="invoice_line_posting_account_ref"):
        _evidence_with(db, manifest(db, invoice_id=str(UUID(int=0xBEEF))))  # a manifest for another invoice


def test_round_2dp_maps_to_the_scale_check_and_nothing_else():
    rounded = InvoiceTransform(frozenset({T_ROUND_2DP}))
    assert rounded.waivable_checks() == {"source_amount_scale": T_ROUND_2DP}
    assert InvoiceTransform(frozenset({T_EXP_LEGACY})).waivable_checks() == {}


def test_a_malformed_manifest_is_refused():
    for data in ({}, {"landed": []}, {"landed": {INVOICE: {"rules": []}}}):
        with pytest.raises(TransformManifestError):
            TransformManifest.from_itemised(data)


def _evidence_with(db, tm):
    db[0].exec_driver_sql("PRAGMA query_only=1")
    verifier = replace(db[3], transform_manifest=tm)
    from test_payables_history import T

    return verifier.evidence(scope=SimpleNamespace(tenant_id=UUID(T)), invoice_id=UUID(INVOICE), destination=db[2])
