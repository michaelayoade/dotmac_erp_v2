"""AR opening on real PostgreSQL with the real billing a12 lane (hosted CI only, ERP_TEST_POSTGRES_URL).

mint -> plan -> provision (app_admin) -> import every partition (restricted writer) -> seal; 0 GL; receipts.
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from ar_opening_support import CUTOFF, SNAPSHOT, UTC, alloc, capture, invoice, payment
from dotmac_billing import get_opening_window, get_opening_window_seal
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.models import Tenant
from sqlalchemy.orm import Session
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.importer.ar_assembly import WRITER_ROLE, ArAssembly, ErpOpeningAuthority
from erp_v2.importer.ar_census import build_census, normalise_rows
from erp_v2.importer.ar_opening_cli import (
    OperatorRefusal,
    apply_opening,
    build_plan,
    existing_account_ids,
    mint_customers_and_accounts,
    role_oid,
)

REF = "dotmac-debt-register v1"
FENCE = "fence-1"
NOW = datetime(2026, 11, 20, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


@pytest.fixture(autouse=True, scope="module")
def _app_admin_provision_grants(engine):
    """Billing's window trigger demands current_user = app_admin. In production app_admin owns mod_billing; in this
    fixture the migration role owns it, so give app_admin exactly what the provision step touches."""

    # The writer role is cluster-global: when an earlier test module's migration role created it, this module's
    # migration role could not be granted membership (see e2_0029); give it SET out of band, as an operator would.
    admin = sa.create_engine(
        sa.make_url(os.environ["ERP_TEST_POSTGRES_URL"]), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    with admin.connect() as c:
        owner = engine.url.username
        if not c.scalar(sa.text("SELECT pg_has_role(:o, :r, 'SET')"), {"o": owner, "r": WRITER_ROLE}):
            c.exec_driver_sql(f'GRANT {WRITER_ROLE} TO "{owner}"')
    admin.dispose()
    with engine.begin() as c:
        c.exec_driver_sql("GRANT USAGE ON SCHEMA mod_billing TO app_admin")
        c.exec_driver_sql("GRANT SELECT ON ALL TABLES IN SCHEMA mod_billing TO app_admin")
        c.exec_driver_sql("GRANT INSERT ON mod_billing.opening_windows TO app_admin")
        c.exec_driver_sql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_admin")


def _rows(variance: bool = False):
    invoices = [
        invoice("inv-1", total="1000.00", dotmac_sub_id="sub-9", posted=True),
        invoice("inv-2", total="500.00", splynx_id="spx-1", posted=True),
        invoice("inv-3", total="40.00", posted=True),
        invoice("inv-closed", total="30.00", posted=True),
        invoice(
            "inv-5",
            customer="cust-2",
            total="75.50",
            posted=True,
            **({"balance_due": Decimal("70.00")} if variance else {}),
        ),
        invoice("cn-1", customer="cust-3", total="-5.00", invoice_type="CREDIT_NOTE", posted=True),
        invoice("inv-6", customer="cust-3", total="10.00", posted=True),
    ]
    payments = [
        payment("pay-1", "450.00", posted=True),
        payment("pay-2", "10.00", posted=True),
        payment("pay-closed", "30.00", posted=True),
        payment("pay-9", "20.00", customer="cust-2", posted=True),
    ]
    allocations = [
        alloc("pay-1", "inv-1", "400.00"),
        alloc("pay-closed", "inv-closed", "30.00"),
        alloc("pay-2", "inv-3", "10.00"),
    ]
    return invoices, payments, allocations


def _world(engine, *, variance: bool = False) -> SimpleNamespace:
    tenant = uuid4()
    with Session(engine) as db:
        db.add(Tenant(id=tenant, slug=f"t{tenant.hex[:24]}", name="AR opening"))
        db.commit()
    scope = TenantScope(tenant)
    invoices, payments, allocations = _rows(variance)
    census = build_census(
        snapshot_sha256="b" * 64,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=payments,
        allocations=allocations,
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    customers = {
        k: {"customer_key": k, "legal_name": f"Customer {k}", "dotmac_sub_id": "ext-1" if k == "cust-1" else None}
        for k in ("cust-1", "cust-2", "cust-3")
    }
    with Session(engine) as db, db.begin():
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        minted = mint_customers_and_accounts(
            db, scope=scope, partitions=list(census.carry_partitions), customers=customers
        )
    ni, np_, na = normalise_rows(invoices, payments, allocations)
    asm = ArAssembly(
        tenant_id=tenant,
        capture=capture(),
        census=census,
        invoices=ni,
        payments=np_,
        allocations=na,
        account_ids=minted,
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    return SimpleNamespace(tenant=tenant, scope=scope, census=census, asm=asm, minted=minted)


def _plan(engine, w):
    with Session(engine) as db, db.begin():
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        return build_plan(w.asm, db, scope=w.scope, migration_authority_ref=REF, occurred_at=NOW, params={}), role_oid(
            db
        )


def _authority(w, oid):
    return ErpOpeningAuthority(
        assembly=w.asm, migration_authority_ref=REF, role_oid=oid, expected_coupled_watermark=FENCE
    )


def _apply(engine, w, plan, oid, **kw):
    return apply_opening(
        engine,
        assembly=w.asm,
        authority=_authority(w, oid),
        plan=plan,
        approved_plan_sha256=kw.pop("approved", plan["plan_sha256"]),
        occurred_at=NOW,
        applied_by="tester",
        coupled_watermark=kw.pop("fence", FENCE),
        env=kw.pop("env", {"ERP_AR_OPENING_APPLY": "1"}),
    )


def test_census_excludes_the_credit_note_partition_and_mint_creates_only_carryable_accounts(engine):
    w = _world(engine)
    assert w.census.carry_partitions == (("cust-1", "NGN"), ("cust-2", "NGN"))
    assert [e.partition for e in w.census.excluded] == [("cust-3", "NGN")]
    with Session(engine) as db:
        found = existing_account_ids(
            db, scope=w.scope, partitions=[("cust-1", "NGN"), ("cust-2", "NGN"), ("cust-3", "NGN")]
        )
    assert set(found) == {("cust-1", "NGN"), ("cust-2", "NGN")} and found == w.minted
    with Session(engine) as db, db.begin():  # mint is idempotent
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        again = mint_customers_and_accounts(db, scope=w.scope, partitions=list(w.census.carry_partitions), customers={})
    assert again == w.minted


def test_plan_previews_clean_and_apply_refuses_without_authorisation_or_with_a_wrong_sha(engine):
    w = _world(engine)
    plan, oid = _plan(engine, w)
    assert plan["preview_ok"] and plan["partitions"] == 2 and plan["excluded_partitions"] == 1
    assert plan["receivable_items"] == 4 and plan["cash_lots"] == 3 and plan["variances"] == []
    with pytest.raises(OperatorRefusal, match="explicit_apply_authorization_required"):
        _apply(engine, w, plan, oid, env={})
    with pytest.raises(OperatorRefusal, match="approved_plan_sha_mismatch"):
        _apply(engine, w, plan, oid, approved="0" * 64)
    with Session(engine) as db:  # nothing was provisioned by the refusals
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        assert get_opening_window(db, scope=w.scope) is None


def test_provision_import_seal_with_the_restricted_writer_and_zero_gl(engine):
    w = _world(engine)
    plan, oid = _plan(engine, w)
    receipt = _apply(engine, w, plan, oid)
    assert receipt["sealed"] and len(receipt["admitted"]) == 2
    with Session(engine) as db:
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        window = get_opening_window(db, scope=w.scope)
        assert window is not None and window.principal_name == WRITER_ROLE and window.principal_oid == oid
        assert get_opening_window_seal(db, scope=w.scope) is not None
        # the receivable position equals the census per partition (residual minor / 100)
        total = db.execute(
            sa.text(
                "SELECT coalesce(sum(initial_receivable), 0) FROM mod_billing.opening_receivable_bases "
                "WHERE tenant_id = :t"
            ),
            {"t": w.tenant},
        ).scalar()
        assert int(total * 100) == plan["open_ar_minor"]
        rows = db.execute(
            sa.text("SELECT source_account_key, posting_group_id FROM erp_ar_opening_admissions WHERE tenant_id = :t"),
            {"t": w.tenant},
        ).all()
        assert sorted(r[0] for r in rows) == ["cust-1", "cust-2"]
        # 0 GL: the opening posted no journal at all (and billing posts none by itself)
        journals = db.execute(
            sa.text("SELECT count(*) FROM mod_accounting.journal_entries WHERE tenant_id = :t"), {"t": w.tenant}
        ).scalar()
        assert journals == 0


def test_admission_receipts_are_immutable(engine):
    w = _world(engine)
    plan, oid = _plan(engine, w)
    _apply(engine, w, plan, oid)
    with engine.begin() as c:
        with pytest.raises(sa.exc.DBAPIError, match="immutable"):
            c.execute(
                sa.text("UPDATE erp_ar_opening_admissions SET applied_by = 'x' WHERE tenant_id = :t"), {"t": w.tenant}
            )
    with pytest.raises(sa.exc.DBAPIError, match="immutable"), engine.begin() as c:
        c.execute(sa.text("DELETE FROM erp_ar_opening_admissions WHERE tenant_id = :t"), {"t": w.tenant})


def test_wrong_principal_oid_is_refused_at_provision_and_a_wrong_fence_at_seal(engine):
    w = _world(engine)
    plan, oid = _plan(engine, w)
    with pytest.raises(Exception):  # noqa: B017  (billing refuses: the provision verifier returns None)
        _apply(engine, w, plan, oid + 1)
    w2 = _world(engine)
    plan2, oid2 = _plan(engine, w2)
    with pytest.raises(OperatorRefusal, match="seal_refused"):
        _apply(engine, w2, plan2, oid2, fence="some-other-fence")


def test_the_writer_role_is_nologin_unprivileged_and_not_an_ordinary_member(engine):
    with engine.connect() as c:
        row = c.execute(
            sa.text(
                "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = :n"
            ),
            {"n": WRITER_ROLE},
        ).one()
        assert tuple(row) == (False, False, False, False, False, False)
        assert c.scalar(sa.text("SELECT pg_has_role(:n, 'app_user', 'MEMBER')"), {"n": WRITER_ROLE}) is False
        assert c.scalar(sa.text("SELECT pg_has_role(:n, 'platform_api', 'MEMBER')"), {"n": WRITER_ROLE}) is False
        assert c.scalar(sa.text("SELECT pg_has_role(:n, 'app_admin', 'MEMBER')"), {"n": WRITER_ROLE}) is False
        assert UUID(int=0) is not None and uuid4() is not None


def test_variances_are_carried_in_the_manifest_and_apply_refuses_them_unless_explicitly_allowed(engine):
    w = _world(engine, variance=True)
    plan, oid = _plan(engine, w)
    assert plan["preview_ok"]
    assert [(v["partition"], v["reason"], v["amount_minor"]) for v in plan["variances"]] == [
        ("cust-2", "stored_balance_difference", -550)
    ]
    with pytest.raises(OperatorRefusal, match="variances_need_post_seal_resolution"):
        _apply(engine, w, plan, oid)
    receipt = _apply(
        engine, w, plan, oid, env={"ERP_AR_OPENING_APPLY": "1", "ERP_AR_OPENING_ALLOW_OPEN_VARIANCES": "1"}
    )
    assert receipt["sealed"]
    with Session(engine) as db:  # recorded by billing at the seal, awaiting post-seal resolution
        count = db.execute(
            sa.text("SELECT count(*) FROM mod_billing.opening_variances WHERE tenant_id = :t"), {"t": w.tenant}
        ).scalar()
        assert count == 1
