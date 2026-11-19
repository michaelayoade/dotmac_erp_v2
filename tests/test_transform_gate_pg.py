# ruff: noqa: F811  (imported fixtures are used by name)
"""B-3 transform manifest through B1b against real PostgreSQL (hosted CI only): D-M2 APPROVED-unposted invoices and
item-linked lines, in the four combinations (transform alone, waiver alone, both, transform that does not cover)."""

from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_payables.models import SupplierInvoice, SupplierInvoiceLine
from sqlalchemy.orm import Session
from test_b1b_recognize_pg import (  # noqa: F401  (fixtures and helpers of the B1b suite)
    TAX_CODE,
    audit_registry,
    cli,
    counts,
    grant,
    imported,
    setup,
)
from test_reviewed_admission_pg import record, row, zero_allocation

from erp_v2.importer.transforms import T_ITEM_NONSTOCK, T_UNPOSTED_CORRECT
from erp_v2.legacy_ap.transform_manifest import TransformManifest

engine = bridge_pg.engine
ITEM = "00000000-0000-0000-0000-0000000000a3"


def dest_line(engine, iid):
    with Session(engine) as db:
        line = db.scalar(sa.select(SupplierInvoiceLine).where(SupplierInvoiceLine.invoice_id == iid))
        inv = db.get(SupplierInvoice, iid)
        return dict(id=str(line.id), posting=line.posting_account_ref, amount=line.line_amount, sub=inv.subtotal)


def manifest(iid, engine, *, rules, items=(), header=None, lines=None):
    landed = dict(rules=sorted(rules), header=header or {}, lines=lines or {}, item_lines=list(items))
    return TransformManifest.from_itemised({"landed": {str(iid): landed}})


def plan(legacy, engine, iid, tm, monkeypatch):
    original = cli.run_cohort
    with monkeypatch.context() as patched:  # scoped: later runs in the same test have no manifest
        patched.setattr(cli, "run_cohort", lambda *a, **k: original(*a, **{**k, "transform_manifest": tm}))
        return legacy.run(engine, (iid,), apply=False)


# --- item-linked lines (T-ITEM-NONSTOCK) ---------------------------------------------------------------------------


def test_item_link_is_refused_without_a_manifest_and_admitted_with_the_exact_one(engine, setup, monkeypatch):
    env, legacy = setup
    iid = imported(engine, env)
    legacy.line_overrides[iid] = dict(item_id=ITEM)
    legacy.build((iid,))
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"legacy_evidence:source_item_link": 1}
    line = dest_line(engine, iid)
    found = plan(legacy, engine, iid, manifest(iid, engine, rules=[T_ITEM_NONSTOCK], items=[line["id"]]), monkeypatch)
    assert found["counts"]["eligible"] == 1 and found["refusals_by_reason"] == {} and found["waived"] == {}


def test_a_transform_that_does_not_cover_the_item_line_is_still_refused(engine, setup, monkeypatch):
    env, legacy = setup
    iid = imported(engine, env)
    legacy.line_overrides[iid] = dict(item_id=ITEM)
    legacy.build((iid,))
    for tm in (
        manifest(iid, engine, rules=[T_UNPOSTED_CORRECT], items=[]),  # another rule, no item application
        manifest(iid, engine, rules=[T_ITEM_NONSTOCK], items=[str(UUID(int=0xABC))]),  # another line
    ):
        refused = plan(legacy, engine, iid, tm, monkeypatch)["refusals_by_reason"]
        assert refused == {"legacy_evidence:source_item_link": 1}


def test_waiver_alone_does_not_admit_an_item_link_and_transform_alone_does_not_admit_the_allocation(
    engine, setup, monkeypatch
):
    env, legacy = setup
    iid = imported(engine, env)
    zero_allocation(legacy, iid)
    legacy.line_overrides[iid] = dict(item_id=ITEM)
    legacy.build((iid,))
    line = dest_line(engine, iid)
    tm = manifest(iid, engine, rules=[T_ITEM_NONSTOCK], items=[line["id"]])
    # transform alone: the allocation (a waivable check) is still not admitted
    assert plan(legacy, engine, iid, tm, monkeypatch)["refusals_by_reason"] == {"legacy_evidence:source_allocations": 1}
    # waiver alone: the item link (not waivable) still refuses
    record(engine, env, [row(iid, waived=("source_allocations",))])
    assert legacy.run(engine, (iid,), apply=False)["refusals_by_reason"] == {"legacy_evidence:source_item_link": 1}


def test_transform_plus_waiver_admits_and_both_are_reported_apart(engine, setup, monkeypatch):
    env, legacy = setup
    iid = imported(engine, env)
    zero_allocation(legacy, iid)
    legacy.line_overrides[iid] = dict(item_id=ITEM)
    legacy.build((iid,))
    line = dest_line(engine, iid)
    record(engine, env, [row(iid, waived=("source_allocations",))])
    tm = manifest(iid, engine, rules=[T_ITEM_NONSTOCK], items=[line["id"]])
    found = plan(legacy, engine, iid, tm, monkeypatch)
    assert found["counts"]["eligible"] == 1 and found["refusals_by_reason"] == {}
    assert found["waived"] == {"source_allocations": 1}  # the waiver is reported; the transform is not a waiver
    before = counts(engine, env.tenant_id)
    assert before  # plan mode wrote nothing


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
    return iid, line, gross


def landed(line, gross, *, total=None):
    return dict(
        header=dict(subtotal=str(gross), tax_amount="0", total_amount=str(total if total is not None else gross)),
        lines={
            line["id"]: dict(
                posting_account_ref=line["posting"], tax_account_ref=None, line_amount=str(gross), tax_amount="0"
            )
        },
    )


def test_d_m2_approved_unposted_wht_invoice_is_refused_without_and_passes_with_the_manifest(engine, setup, monkeypatch):
    env, legacy = setup
    iid, line, gross = wht_world(engine, env, legacy)
    assert legacy.run(engine, (iid,), apply=False)["counts"]["eligible"] == 0  # the destination differs from source
    tm = manifest(iid, engine, rules=[T_UNPOSTED_CORRECT], **landed(line, gross))
    found = plan(legacy, engine, iid, tm, monkeypatch)
    assert found["counts"]["eligible"] == 1 and found["refusals_by_reason"] == {} and found["waived"] == {}


def test_d_m2_manifest_that_names_other_values_than_the_destination_is_refused(engine, setup, monkeypatch):
    env, legacy = setup
    iid, line, gross = wht_world(engine, env, legacy)
    wrong = manifest(iid, engine, rules=[T_UNPOSTED_CORRECT], **landed(line, gross, total=gross + 10))
    refused = plan(legacy, engine, iid, wrong, monkeypatch)["refusals_by_reason"]
    assert list(refused) == ["legacy_evidence:header_total_amount"]
