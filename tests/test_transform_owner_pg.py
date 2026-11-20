# ruff: noqa: F811  (imported fixtures are used by name)
"""B-3 transforms through the OWNER calls (settled and unpaid admission) and forged manifests in all three verifiers,
against real PostgreSQL (hosted CI only). The manifest here is GENUINE: built by the importer's own pure build over
the very snapshot file the verifiers read. A forged manifest over an edited destination line is refused."""

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
import test_settled_admission_pg as settled_pg
from dotmac_payables import admit_historical_unpaid_invoice
from dotmac_payables.models import PaymentObligation, SupplierInvoiceLine
from sqlalchemy.orm import Session
from test_b1b_recognize_pg import audit_registry, grant  # noqa: F401
from test_settled_admission_pg import SNAP, hosted_postgres, one, setup  # noqa: F401

from erp_v2.importer.payables_history import HistoricalPayablesVerifier
from erp_v2.importer.tax_accounts import (
    DEFERRAL_JOURNAL_SQL,
    GLOBAL_TAX_ACCOUNT_SQL,
    INVOICE_LINE_SQL,
    LEDGER_DEBIT_SQL,
)
from erp_v2.importer.transforms import (
    ACCOUNT_SQL,
    INVOICE_HEADER_SQL,
    LINE_ITEM_SQL,
    T_ROUND_2DP,
    TransformParams,
    build_transforms,
)
from erp_v2.legacy_ap import settled_cli
from erp_v2.legacy_ap.transform_manifest import TransformManifest

engine = bridge_pg.engine
FALLBACK = str(UUID(int=0x5999))
ROUNDED_JOURNAL = "200.004"
ACCOUNT_COLUMNS = ["account_id", "deferral_pair_account_id", "account_code", "is_active", "is_posting_allowed"]


@pytest.fixture
def three_places(monkeypatch):
    """The legacy GL (and so the destination journals, which carry it unchanged) holds 200.004 for the invoice;
    the subledger invoice lands rounded to 200.00."""
    original = settled_pg.seed_journal

    def seed(engine, env, *, number, document_type, document_id, lines):
        if document_type == "SUPPLIER_INVOICE":
            lines = [("5000", ROUNDED_JOURNAL, "0"), ("2000", "0", ROUNDED_JOURNAL)]
        return original(engine, env, number=number, document_type=document_type, document_id=document_id, lines=lines)

    monkeypatch.setattr(settled_pg, "seed_journal", seed)
    monkeypatch.setitem(settled_pg.SOURCE_TABLES, "account", ACCOUNT_COLUMNS)


def over_two_places(scene, iid):
    scene.edit("supplier_invoice", "invoice_id", iid, subtotal=ROUNDED_JOURNAL, total_amount=ROUNDED_JOURNAL)
    scene.edit("supplier_invoice", "invoice_id", iid, functional_currency_amount=ROUNDED_JOURNAL)
    scene.edit("supplier_invoice_line", "invoice_id", iid, line_amount=ROUNDED_JOURNAL)
    scene.rows["account"] = [
        dict(account_id=a, deferral_pair_account_id=None, account_code=c, is_active="1", is_posting_allowed="1")
        for a, c in ((str(scene.env.accounts["5000"]), "5000"), (str(scene.env.accounts["2000"]), "2000"))
    ]


def importer_manifest(scene) -> TransformManifest:
    """The importer's own build over the snapshot file (the same SQL it runs against the real snapshot)."""
    source = sa.create_engine(f"sqlite:///{scene.path}")
    try:
        with source.connect() as conn:

            def rows(sql):
                return [dict(r) for r in conn.execute(sa.text(sql)).mappings()]

            chart = [
                dict(
                    id=FALLBACK,
                    tenant_id=str(scene.env.tenant_id),
                    code="5999",
                    is_active=True,
                    posting_allowed=True,
                    account_class="EXPENSE",
                )
            ]
            build = build_transforms(
                rows(INVOICE_LINE_SQL),
                rows(LEDGER_DEBIT_SQL),
                rows(GLOBAL_TAX_ACCOUNT_SQL),
                rows(DEFERRAL_JOURNAL_SQL),
                rows(INVOICE_HEADER_SQL),
                rows(LINE_ITEM_SQL),
                rows(ACCOUNT_SQL),
                TransformParams(FALLBACK, str(scene.env.tenant_id), "remove"),  # the D-M2 WHT removal under test
                chart,
                {"snapshot_sha256": SNAP},
            )
    finally:
        source.dispose()
    return TransformManifest.from_itemised(json.loads(json.dumps(build.ledger.itemised())))


def run_with(scene, ids, tm, monkeypatch, *, apply, digest=None):
    original = settled_cli.run_cohort
    with monkeypatch.context() as patched:
        patched.setattr(
            settled_cli,
            "run_cohort",
            lambda *a, **k: original(*a, **{**k, "transform_manifest": tm, "fallback_expense_account": FALLBACK}),
        )
        return scene.run(ids, apply=apply, digest=digest)


# --- settled admission + T-ROUND-2DP, through the owner ---------------------------------------------------------------


def test_settled_round_2dp_reaches_the_owner_with_the_landed_totals(engine, setup, three_places, monkeypatch):
    _env, scene = setup
    iid = scene.add("AP-1", paid="80")
    over_two_places(scene, iid)
    scene.build()
    tm = importer_manifest(scene)
    entry = tm.for_invoice(iid)
    assert entry.rules == {T_ROUND_2DP} and entry.header["total_amount"] == Decimal("200.00")
    # without the manifest the over-scale source is refused; with it the plan is eligible
    assert scene.run([iid], apply=False)["counts"]["refused"] == 1
    plan = run_with(scene, [iid], tm, monkeypatch, apply=False)
    assert plan["counts"]["eligible"] == 1, plan
    applied = run_with(scene, [iid], tm, monkeypatch, apply=True, digest=plan["plan_digest"])
    assert applied["applied"] == 1
    with Session(engine) as db:
        obligation = db.scalar(sa.select(PaymentObligation).where(PaymentObligation.invoice_id == iid))
        assert obligation.original_amount == Decimal("200.00") and obligation.outstanding_amount == Decimal("120.00")


# --- unpaid admission + T-ROUND-2DP, through the owner ----------------------------------------------------------------


def unpaid_scene(scene, iid):
    """The settled scene's invoice with no payment: the unpaid shape (amount_paid 0, no allocation)."""
    scene.rows["payment_allocation"].clear()
    scene.rows["supplier_payment"].clear()
    scene.rows["journal_entry"][:] = [
        r for r in scene.rows["journal_entry"] if r["source_document_type"] != "SUPPLIER_PAYMENT"
    ]
    scene.edit("supplier_invoice", "invoice_id", iid, amount_paid="0", status="POSTED")


def test_unpaid_round_2dp_reaches_the_owner_with_the_landed_totals(engine, setup, three_places, monkeypatch):
    env, scene = setup
    iid = scene.add("AP-1", paid="80")
    over_two_places(scene, iid)
    unpaid_scene(scene, iid)
    scene.build()
    tm = importer_manifest(scene)
    source = sa.create_engine(f"sqlite:///{scene.path}")
    try:
        with source.connect() as src, Session(engine) as db, db.begin():
            src.exec_driver_sql("PRAGMA query_only=1")
            verifier = HistoricalPayablesVerifier(
                src, scene.manifest(), transform_manifest=tm, fallback_expense_account=FALLBACK
            )
            scope = SimpleNamespace(tenant_id=env.tenant_id)
            evidence = verifier.evidence(scope=scope, invoice_id=iid, destination=db)
            assert evidence.total_amount == Decimal("200.00") and evidence.subtotal == Decimal("200.00")
            admit_historical_unpaid_invoice(
                db,
                scope=scope,
                command=verifier.command(evidence),
                verifier=verifier,
                idempotency_key="k-unpaid-round",
                idempotency_expires_at=None,
                admitted_at=settled_pg.NOW,
            )
            obligation = db.scalar(sa.select(PaymentObligation).where(PaymentObligation.invoice_id == iid))
            assert obligation.original_amount == Decimal("200.00")
            db.rollback()
    finally:
        source.dispose()


# --- forged manifests: an edited destination line plus a matching manifest is refused in all three verifiers ----------


def forge(engine, iid, scene):
    """The destination line differs from the source (submitted payables lines are immutable, so the source is the
    side edited here) and a forged manifest names the destination's values to launder the difference."""
    other = str(UUID(int=0x5100))
    scene.edit("supplier_invoice_line", "invoice_id", iid, expense_account_id=other)
    with Session(engine) as db:
        line = db.scalar(sa.select(SupplierInvoiceLine).where(SupplierInvoiceLine.invoice_id == iid))
        landed = {"posting_account_ref": line.posting_account_ref, "tax_account_ref": None}
        landed |= {"line_amount": str(line.line_amount), "tax_amount": "0"}
        line_id = str(line.id)
    body = {
        "binding": {"snapshot_sha256": SNAP},
        "landed": {str(iid): {"rules": [T_ROUND_2DP], "header": {}, "lines": {line_id: landed}, "item_lines": []}},
        "fallback_account": FALLBACK,
    }
    return TransformManifest.from_itemised(body)


def test_forged_manifest_is_refused_by_settled_and_unpaid(engine, setup, three_places, monkeypatch):
    env, scene = setup
    iid = scene.add("AP-1", paid="80")
    over_two_places(scene, iid)
    forged = forge(engine, iid, scene)
    scene.build()
    plan = run_with(scene, [iid], forged, monkeypatch, apply=False)
    assert plan["refusals_by_reason"] == {"legacy_evidence:transform_manifest_mismatch": 1}
    # the unpaid verifier on the same snapshot
    unpaid_scene(scene, iid)
    scene.build()
    source = sa.create_engine(f"sqlite:///{scene.path}")
    try:
        with source.connect() as src, Session(engine) as db, db.begin():
            src.exec_driver_sql("PRAGMA query_only=1")
            verifier = HistoricalPayablesVerifier(
                src, scene.manifest(), transform_manifest=forged, fallback_expense_account=FALLBACK
            )
            with pytest.raises(ValueError, match="transform_manifest_mismatch"):
                verifier.evidence(scope=SimpleNamespace(tenant_id=env.tenant_id), invoice_id=iid, destination=db)
            db.rollback()
    finally:
        source.dispose()
