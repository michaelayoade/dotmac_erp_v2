"""F6: the REAL S8 fragment and apply receipt (``claims_opening.core`` on main) feed this producer unchanged.

``core.fragment_2040`` and ``core.apply_receipt`` run against a real destination (the S8 scene: admitted 2040-carried
claim, a refused 2040-carried claim in ``held_2040``); the outputs go through ``gl_attribution.parse_fragment``,
``attribution_cli._check_s8_receipt`` and the register adapter ``from_claims_opening``. Hosted CI only.
"""

import json
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_claims_opening_pg import (  # noqa: F401  (hosted-Postgres fixtures and the S8 scene)
    CAPTURE,
    CUTOFF,
    NOW,
    OP,
    REVISION,
    SNAP,
    WINDOW,
    Scene,
    audit_registry,
    world,
)
from test_journals_pg import _registries  # noqa: F401  (hosted-Postgres fixture)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.claims_opening import core, reclass
from erp_v2.claims_opening.verifier import ClaimOpeningVerifier
from erp_v2.holdings.attribution import Party
from erp_v2.migration_ledger import attribution_cli as acli
from erp_v2.migration_ledger import gl_attribution as ga
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import LedgerRefused, sha256_bytes

D = Decimal


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


def real_apply(scene):
    """Plan then apply exactly as ``claims_opening.cli`` does, with the CLI's real binding keys."""
    bindings = dict(
        tenant_id=str(scene.env.tenant_id), snapshot_sha256=SNAP, candidate_revision=REVISION,
        cutoff_receipt_sha256="c" * 64, history_cutoff=CUTOFF.isoformat(), operator_ref=OP, expenses_version="x",
    )  # fmt: skip
    source = sa.create_engine(f"sqlite:///{scene.path}")
    try:
        with source.connect() as src:
            src.exec_driver_sql("PRAGMA query_only=1")
            verifier = ClaimOpeningVerifier(src, scene.manifest(), CUTOFF, OP)
            kw = dict(window_id=WINDOW, capture_date=CAPTURE, bindings=bindings)
            with Session(scene.engine) as db, db.begin():
                plan = core.build_plan(db, src, verifier, scene.env.tenant_id, admitted_at=NOW, **kw)
            with Session(scene.engine) as db, db.begin():
                return core.apply_plan(
                    db, src, verifier, scene.env.tenant_id, reviewed_digest=plan["digest"], now=NOW, **kw
                )
    finally:
        source.dispose()


def test_f6_the_real_s8_fragment_and_apply_receipt_feed_the_producer_unchanged(engine, tmp_path):
    env = world(engine)
    scene = Scene(engine, env, tmp_path)
    admitted_employee = scene.employee()
    refused_employee = scene.employee()
    scene.claim(net="250.00", carried="2040", employee=admitted_employee)
    refused = scene.claim(net="75.00", carried="2040", net_null=True, dest=False, employee=refused_employee)
    scene.claim(net="10.00", carried="2110")
    scene.build()
    result = real_apply(scene)
    with Session(engine) as db:
        journal = reclass.posted_reclass(db, env.tenant_id)
        dr_2040 = core.journal_dr(db, env.tenant_id, journal.id, env.accounts["2040"])
        journal_id = str(journal.id)
    assert dr_2040 == D("250.00")
    fragment = core.fragment_2040(result["admitted_items"], result["items"], dr_2040)
    fragment_bytes = core.dumps(fragment)
    receipt = core.apply_receipt(
        result, mode="apply", attribution_sha256="a" * 64, fragment_sha256=sha256_bytes(fragment_bytes)
    )
    receipt_bytes = core.dumps(receipt)

    # parse_fragment accepts S8's real fragment: per legacy employee id, and F2's N row held on 2040
    per, held, unkeyed = ga.parse_fragment(json.loads(fragment_bytes))
    assert per == {admitted_employee: D("250.00")} and unkeyed == 0
    assert [(h.party, h.class_code, h.legacy_key, h.amount) for h in held] == [
        (Party("employee", refused_employee), "E37", f"claim:{refused}", D("-75.00"))
    ]

    # the receipt binds: kind, self sha, tenant, snapshot, fragment sha and the single v2 reclass journal
    tenant = env.tenant_id
    acli._check_s8_receipt(
        receipt_bytes, sha256_bytes(fragment_bytes), tenant=tenant, snapshot_sha=SNAP, v2_journals=[journal_id]
    )
    with pytest.raises(LedgerRefused) as e:
        acli._check_s8_receipt(
            receipt_bytes, sha256_bytes(fragment_bytes), tenant=tenant, snapshot_sha="0" * 64, v2_journals=[journal_id]
        )
    assert e.value.code == "s8_receipt_not_bound"
    with pytest.raises(LedgerRefused) as e:
        acli._check_s8_receipt(
            receipt_bytes, sha256_bytes(fragment_bytes), tenant=tenant, snapshot_sha=SNAP, v2_journals=["other"]
        )
    assert e.value.code == "s8_receipt_not_bound"

    # the register rows S8 loads agree with the fragment's held rows (same key, class and GL-signed amount)
    facts = {f.legacy_key: f for f in register.from_claims_opening(receipt)}
    row = facts[f"claim:{refused}"]
    assert row.class_code == "E37" and row.detail["gl_signed_minor"] == -7500
    assert D(row.detail["gl_signed_minor"]).scaleb(-2) == held[0].amount
    assert row.detail["employee_id"] == refused_employee
