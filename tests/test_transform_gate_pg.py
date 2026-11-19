# ruff: noqa: F811  (imported fixtures are used by name)
"""B-3 transform manifest through B1b against real PostgreSQL (hosted CI only): D-M2 APPROVED-unposted invoices and
item-linked lines in the four combinations (transform alone, waiver alone, both, transform that does not cover), and
a forged manifest. The manifest is GENUINE: built by the importer's own pure build over the snapshot file."""

from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
import test_b1b_recognize_pg as b1b
import test_posting_bridge_pg as bridge_pg
from dotmac_payables.models import SupplierInvoiceLine
from sqlalchemy.orm import Session
from test_b1b_recognize_pg import TAX_CODE, audit_registry, cli, grant, imported, setup  # noqa: F401
from test_reviewed_admission_pg import record, row
from test_transform_owner_pg import ACCOUNT_COLUMNS, FALLBACK, importer_manifest

from erp_v2.importer.transforms import T_ITEM_NONSTOCK, T_UNPOSTED_CORRECT
from erp_v2.legacy_ap.transform_manifest import TransformManifest

engine = bridge_pg.engine
ITEM = "00000000-0000-0000-0000-0000000000a3"


@pytest.fixture(autouse=True)
def account_columns(monkeypatch):
    monkeypatch.setitem(b1b.SOURCE_TABLES, "account", ACCOUNT_COLUMNS)


def allocation(legacy, iid):
    """An allocation of an unposted legacy payment: the only kind a waiver may admit."""
    pay = str(UUID(int=0xABCDEF))
    legacy.allocations.append(dict(invoice_id=str(iid), payment_id=pay))
    legacy.payments.append(dict(payment_id=pay, status="DRAFT", journal_entry_id=None))


def dest_line(engine, iid):
    with Session(engine) as db:
        line = db.scalar(sa.select(SupplierInvoiceLine).where(SupplierInvoiceLine.invoice_id == iid))
        return dict(id=str(line.id), posting=line.posting_account_ref, amount=line.line_amount)


def plan(legacy, engine, iid, tm, monkeypatch):
    original = cli.run_cohort
    with monkeypatch.context() as patched:  # scoped: later runs in the same test have no manifest
        patched.setattr(
            cli,
            "run_cohort",
            lambda *a, **k: original(*a, **{**k, "transform_manifest": tm, "fallback_expense_account": FALLBACK}),
        )
        return legacy.run(engine, (iid,), apply=False)


def genuine(legacy, iid):
    tm = importer_manifest(legacy)
    return tm, tm.for_invoice(iid)


# --- item-linked lines (T-ITEM-NONSTOCK) ---------------------------------------------------------------------------


def test_item_link_is_refused_without_a_manifest_and_admitted_with_the_genuine_one(engine, setup, monkeypatch):
    env, legacy = setup
    iid = imported(engine, env, tax="0")
    legacy.line_overrides[iid] = dict(item_id=ITEM)
    legacy.build((iid,))
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"legacy_evidence:source_item_link": 1}
    tm, entry = genuine(legacy, iid)
    assert entry.rules == {T_ITEM_NONSTOCK} and entry.item_lines == {str(UUID(dest_line(engine, iid)["id"]))}
    found = plan(legacy, engine, iid, tm, monkeypatch)
    assert found["counts"]["eligible"] == 1 and found["refusals_by_reason"] == {} and found["waived"] == {}


def test_a_manifest_missing_the_item_application_is_still_refused(engine, setup, monkeypatch):
    env, legacy = setup
    iid = imported(engine, env, tax="0")
    plain = imported(engine, env, number="AP-2", tax="0")
    legacy.line_overrides[iid] = dict(item_id=ITEM)
    legacy.build(tuple(sorted((iid, plain), key=str)))
    other = importer_manifest(legacy)  # a genuine manifest, but without this invoice's item line
    refused = plan(legacy, engine, plain, other, monkeypatch)  # the plain invoice needs no entry and passes
    assert refused["counts"]["eligible"] == 1
    empty = TransformManifest.from_itemised(
        {"binding": {"snapshot_sha256": b1b.SNAP}, "landed": {}, "fallback_account": FALLBACK}
    )
    assert plan(legacy, engine, iid, empty, monkeypatch)["refusals_by_reason"] == {
        "legacy_evidence:transform_manifest_mismatch": 1
    }


def test_waiver_alone_does_not_admit_an_item_link_and_transform_alone_does_not_admit_the_allocation(
    engine, setup, monkeypatch
):
    env, legacy = setup
    iid = imported(engine, env, tax="0")
    allocation(legacy, iid)
    legacy.line_overrides[iid] = dict(item_id=ITEM)
    legacy.build((iid,))
    tm, _entry = genuine(legacy, iid)
    assert plan(legacy, engine, iid, tm, monkeypatch)["refusals_by_reason"] == {"legacy_evidence:source_allocations": 1}
    record(engine, env, [row(iid, waived=("source_allocations",))])
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"legacy_evidence:source_item_link": 1}


def test_transform_plus_waiver_admits_and_both_are_reported_apart(engine, setup, monkeypatch):
    env, legacy = setup
    iid = imported(engine, env, tax="0")
    allocation(legacy, iid)
    legacy.line_overrides[iid] = dict(item_id=ITEM)
    legacy.build((iid,))
    record(engine, env, [row(iid, waived=("source_allocations",))])
    tm, _entry = genuine(legacy, iid)
    found = plan(legacy, engine, iid, tm, monkeypatch)
    assert found["counts"]["eligible"] == 1 and found["refusals_by_reason"] == {}
    assert found["waived"] == {"source_allocations": 1}  # the waiver is reported; the transform is not a waiver


# --- D-M2 APPROVED-unposted invoice with a WHT line (T-UNPOSTED-CORRECT) --------------------------------------------


def wht_world(engine, env, legacy):
    """Destination: the transformed invoice (no tax). Source: the legacy invoice with a WITHHOLDING line."""
    iid = imported(engine, env, tax="0")
    line = dest_line(engine, iid)
    gross = Decimal(line["amount"])
    legacy.tax_mode = "wht"
    legacy.overrides[iid] = dict(
        tax_amount="10", total_amount=str(gross + 10), functional_currency_amount=str(gross + 10)
    )
    legacy.line_overrides[iid] = dict(tax_amount="10", tax_code_id=TAX_CODE)
    legacy.build((iid,))
    return iid


def test_d_m2_approved_unposted_wht_invoice_passes_b1b_with_the_genuine_manifest(engine, setup, monkeypatch):
    env, legacy = setup
    iid = wht_world(engine, env, legacy)
    assert legacy.run(engine, (iid,), apply=False)["counts"]["eligible"] == 0  # the destination differs from source
    tm, entry = genuine(legacy, iid)
    assert T_UNPOSTED_CORRECT in entry.rules and entry.header["tax_amount"] == 0
    found = plan(legacy, engine, iid, tm, monkeypatch)
    assert found["counts"]["eligible"] == 1 and found["refusals_by_reason"] == {} and found["waived"] == {}


# --- forged manifest -----------------------------------------------------------------------------------------------


def test_forged_manifest_over_a_differing_destination_is_refused_by_b1b(engine, setup, monkeypatch):
    """The destination line differs from the source and a manifest names the destination's values."""
    env, legacy = setup
    iid = imported(engine, env, tax="0")
    line = dest_line(engine, iid)
    legacy.line_overrides[iid] = dict(expense_account_id=str(UUID(int=0x5100)))
    legacy.build((iid,))
    landed = dict(rules=[T_UNPOSTED_CORRECT], header={}, item_lines=[])
    landed["lines"] = {
        line["id"]: dict(
            posting_account_ref=line["posting"], tax_account_ref=None, line_amount=str(line["amount"]), tax_amount="0"
        )
    }
    forged = TransformManifest.from_itemised(
        {"binding": {"snapshot_sha256": b1b.SNAP}, "landed": {str(iid): landed}, "fallback_account": FALLBACK}
    )
    assert plan(legacy, engine, iid, forged, monkeypatch)["refusals_by_reason"] == {
        "legacy_evidence:transform_manifest_mismatch": 1
    }
