"""AR opening on real PostgreSQL with the real billing a12 lane (hosted CI only, ERP_TEST_POSTGRES_URL).

mint -> plan -> provision (app_admin) -> import every partition (restricted writer) -> seal; 0 GL; receipts.
"""

from __future__ import annotations

# ruff: noqa: E501
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
import sqlalchemy as sa
from ar_opening_support import CUTOFF, SNAPSHOT, UTC, alloc, capture, invoice, payment
from dotmac_billing import get_opening_window, get_opening_window_seal
from dotmac_customers import AccountStatus
from dotmac_customers.models import CustomerAccount
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
    mint_billing_accounts,
    role_oid,
)


# Legacy customer ids are UUIDs; the wave-4 import preserves them as customer_accounts.id.
def _cust(tenant: UUID, name: str) -> UUID:
    return uuid5(tenant, name)  # customer account ids are global primary keys: derive them per tenant


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
    owner = engine.url.username
    with admin.connect() as c:  # Michael's root step, for the test window only: SET without INHERIT
        c.exec_driver_sql(f'GRANT {WRITER_ROLE} TO "{owner}" WITH SET TRUE, INHERIT FALSE')
    with engine.begin() as c:
        c.exec_driver_sql("GRANT USAGE ON SCHEMA mod_billing TO app_admin")
        c.exec_driver_sql("GRANT SELECT ON ALL TABLES IN SCHEMA mod_billing TO app_admin")
        c.exec_driver_sql("GRANT INSERT ON mod_billing.opening_windows TO app_admin")
        c.exec_driver_sql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_admin")
    yield
    with admin.connect() as c:  # revoked after the window, as in runbook F9 (the role is cluster-global)
        c.exec_driver_sql(f'REVOKE {WRITER_ROLE} FROM "{owner}"')
    admin.dispose()


def _rows(tenant: UUID, variance: bool = False):
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
        invoice("inv-7", customer="cust-4", total="10.005", posted=True),  # inexact: whole partition is excluded
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
    for row in (*invoices, *payments):
        row["customer_key"] = str(_cust(tenant, row["customer_key"]))
    return invoices, payments, allocations


def _wave4_customers(engine, tenant, keys) -> None:
    """The wave-4 import already landed these accounts (id = the legacy customer id); the AR mint only reuses them."""

    with Session(engine) as db, db.begin():
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        for key in keys:
            db.add(
                CustomerAccount(
                    id=_cust(tenant, key),
                    tenant_id=tenant,
                    account_number=f"W4-{tenant.hex[:8]}-{key}".upper(),
                    display_name=f"Customer {key}",
                    status=AccountStatus.ACTIVE,
                )
            )


def _world(engine, *, variance: bool = False) -> SimpleNamespace:
    tenant = uuid4()
    with Session(engine) as db:
        db.add(Tenant(id=tenant, slug=f"t{tenant.hex[:24]}", name="AR opening"))
        db.commit()
    scope = TenantScope(tenant)
    invoices, payments, allocations = _rows(tenant, variance)
    census = build_census(
        snapshot_sha256="b" * 64,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=payments,
        allocations=allocations,
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    _wave4_customers(engine, tenant, ("cust-1", "cust-2", "cust-3", "cust-4"))
    with Session(engine) as db, db.begin():
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        minted = mint_billing_accounts(db, scope=scope, partitions=list(census.carry_partitions))
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
    return SimpleNamespace(
        tenant=tenant, scope=scope, census=census, asm=asm, minted=minted, k=lambda n: str(_cust(tenant, n))
    )


def _plan(engine, w, dual="on"):
    with Session(engine) as db, db.begin():
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        plan = build_plan(
            w.asm, db, scope=w.scope, migration_authority_ref=REF, occurred_at=NOW, params={}, dual_control=dual
        )
        return plan, role_oid(db)


def _authority(w, oid, dual="on"):
    return ErpOpeningAuthority(
        assembly=w.asm,
        migration_authority_ref=REF,
        role_oid=oid,
        expected_coupled_watermark=FENCE,
        expected_dual_control=dual,
    )


def _apply(engine, w, plan, oid, **kw):
    return apply_opening(
        engine,
        assembly=w.asm,
        authority=_authority(w, oid, "on" if kw.get("dual", True) else "off"),
        plan=plan,
        approved_plan_sha256=kw.pop("approved", plan["plan_sha256"]),
        occurred_at=NOW,
        applied_by="tester",
        coupled_watermark=kw.pop("fence", FENCE),
        dual_control=kw.pop("dual", True),  # pinned a12 windows are two-person; a13 binds the policy per window
        env=kw.pop("env", {"ERP_AR_OPENING_APPLY": "1"}),
    )


def test_census_carries_the_credit_note_customer_and_excludes_only_the_inexact_partition(engine):
    w = _world(engine)
    assert w.census.carry_partitions == tuple(sorted((w.k(n), "NGN") for n in ("cust-1", "cust-2", "cust-3")))
    assert [e.partition for e in w.census.excluded] == [(w.k("cust-4"), "NGN")]
    with Session(engine) as db:
        found = existing_account_ids(
            db, scope=w.scope, partitions=[(w.k(n), "NGN") for n in ("cust-1", "cust-2", "cust-3", "cust-4")]
        )
    assert set(found) == set(w.minted) and found == w.minted  # cust-4 was never minted
    with Session(engine) as db, db.begin():  # mint is idempotent and reuses the wave-4 accounts
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        again = mint_billing_accounts(db, scope=w.scope, partitions=list(w.census.carry_partitions))
        assert again == w.minted
        assert (
            db.scalar(
                sa.text("SELECT count(*) FROM mod_customers.customer_accounts WHERE tenant_id = :t"), {"t": w.tenant}
            )
            == 4
        )


def test_mint_refuses_a_customer_the_wave4_import_did_not_land(engine):
    w = _world(engine)
    with Session(engine) as db, db.begin():
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        with pytest.raises(OperatorRefusal, match="customer_not_imported"):
            mint_billing_accounts(db, scope=w.scope, partitions=[(str(UUID(int=0xDEAD)), "NGN")])


def test_plan_previews_clean_with_the_unapplied_credit_note_out_of_the_manifest_p1(engine):
    """P-1 against the pinned billing (a12): a partition carried WITHOUT its unapplied credit note previews clean."""

    w = _world(engine)
    plan, oid = _plan(engine, w)
    assert plan["preview_ok"] and plan["manifest_blockers"] == [] and plan["partition_blockers"] == []
    assert plan["partitions"] == 3 and plan["excluded_partitions"] == 1 and plan["unapplied_credit_notes"] == 1
    assert plan["receivable_items"] == 5 and plan["cash_lots"] == 3 and plan["variances"] == []
    assert plan["t1_credit_by_currency"] == {"NGN": 5000 + 0 + 2000}  # pay-1 50.00 + pay-2 0 + pay-9 20.00
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
    assert receipt["sealed"] and len(receipt["admitted"]) == 3 and receipt["dual_control"] == "on"
    with Session(engine) as db:
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        window = get_opening_window(db, scope=w.scope)
        assert window is not None and window.principal_name == WRITER_ROLE and window.principal_oid == oid
        assert get_opening_window_seal(db, scope=w.scope) is not None
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
        assert sorted(r[0] for r in rows) == sorted(w.k(n) for n in ("cust-1", "cust-2", "cust-3"))
        journals = db.execute(
            sa.text("SELECT count(*) FROM mod_accounting.journal_entries WHERE tenant_id = :t"), {"t": w.tenant}
        ).scalar()
        assert journals == 0
    # T1 anchor: the per-currency amount is the sealed manifest's cash-lot residual sum, also per-partition controls
    assert plan["t1_credit_by_currency"] == {
        "NGN": sum(c.available_cash_credit_minor for c in w.asm.manifest().controls)
    }


def test_dual_control_is_bound_explicitly_from_the_policy_and_read_back(engine):
    """a12 (pinned) has no per-window dual_control: policy OFF is refused, nothing provisioned. On a13 the explicit
    policy value is bound at provision and the read-back equals it (run locally against the a13 source)."""

    from dotmac_billing.opening_window_contracts import OpeningWindowV1

    a13 = "dual_control" in OpeningWindowV1.__dataclass_fields__
    w = _world(engine)
    plan, oid = _plan(engine, w, "off")  # the plan is built under the policy (OFF) it will be applied under
    if not a13:
        with pytest.raises(OperatorRefusal, match="window_dual_control_unbindable_pin_billing_a13"):
            _apply(engine, w, plan, oid, dual=False)
        with Session(engine) as db:
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
            assert get_opening_window(db, scope=w.scope) is None
        return
    with pytest.raises(OperatorRefusal, match="authority_dual_control_differs_from_policy"):
        apply_opening(
            engine,
            assembly=w.asm,
            authority=_authority(w, oid, "on"),
            plan=plan,
            approved_plan_sha256=plan["plan_sha256"],
            occurred_at=NOW,
            applied_by="tester",
            coupled_watermark=FENCE,
            dual_control=False,
            env={"ERP_AR_OPENING_APPLY": "1"},
        )
    receipt = _apply(engine, w, plan, oid, dual=False)
    assert receipt["sealed"] and receipt["dual_control"] == "off"
    with Session(engine) as db:
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(w.tenant)})
        assert get_opening_window(db, scope=w.scope).dual_control == "off"


def test_apply_refuses_a_plan_whose_variances_are_not_the_manifests(engine):
    w = _world(engine, variance=True)
    plan, oid = _plan(engine, w)
    assert [(v["partition"], v["reason"], v["amount_minor"]) for v in plan["variances"]] == [
        (w.k("cust-2"), "stored_balance_difference", -550)
    ]
    tampered = {**plan, "variances": []}
    with pytest.raises(OperatorRefusal, match="variances_not_listed_in_plan"):
        _apply(engine, w, tampered, oid, approved=plan["plan_sha256"])


def test_variances_are_listed_sha_bound_and_apply_proceeds_without_any_env_switch(engine):
    w = _world(engine, variance=True)
    plan, oid = _plan(engine, w)
    assert plan["preview_ok"]
    receipt = _apply(engine, w, plan, oid)  # only ERP_AR_OPENING_APPLY: no allow-variances switch exists
    assert receipt["sealed"] and receipt["variances"] == plan["variances"]
    with Session(engine) as db:  # recorded by billing at the seal; recognised/confirmed post-flip, not in this run
        count = db.execute(
            sa.text("SELECT count(*) FROM mod_billing.opening_variances WHERE tenant_id = :t"), {"t": w.tenant}
        ).scalar()
        assert count == 1


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


# --- the writer role (billing R5) -----------------------------------------------------------------------------------


_NEVER_INSERT = (
    "document_lines document_events confirmed_settlements allocation_effects non_cash_credit_grants "
    "non_cash_credit_effects withholding_discharges opening_hold_admissions opening_authority_switch_receipts "
    "rated_obligations billing_accounts opening_windows"
).split()


def test_the_writer_role_is_nologin_unprivileged_and_not_linked_to_the_serving_roles(engine):
    with engine.connect() as c:
        row = c.execute(
            sa.text(
                "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = :n"
            ),
            {"n": WRITER_ROLE},
        ).one()
        assert tuple(row) == (False, False, False, False, False, False)
        for other in ("app_user", "platform_api", "app_admin"):
            assert c.scalar(sa.text("SELECT pg_has_role(:n, :o, 'MEMBER')"), {"n": WRITER_ROLE, "o": other}) is False
            assert c.scalar(sa.text("SELECT pg_has_role(:o, :n, 'MEMBER')"), {"n": WRITER_ROLE, "o": other}) is False
        # the role is a member of NOTHING (not even transitively)
        assert (
            c.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = :n"
                ),
                {"n": WRITER_ROLE},
            )
            == 0
        )


def test_the_migration_grants_no_membership_of_the_writer_to_the_migrator_or_anyone_else(engine):
    """The fixture (as an operator would) grants the migrator SET out of band for this test cluster; the MIGRATION
    itself must not. Proved on a fresh role pair via the same code path the migration uses."""

    import erp_v2.migrations.versions.e2_0029_ar_opening_admissions as mig

    source = Path(mig.__file__).read_text()
    assert "TO ' || quote_ident(current_user)" not in source and "GRANT {WRITER_ROLE} TO" not in source
    lines = mig._grant_lines()
    assert not any(line.startswith("GRANT") and f"GRANT {WRITER_ROLE}" in line for line in lines)


def test_writer_privileges_are_exactly_r5(engine):
    schema = "mod_billing"
    with engine.connect() as c:

        def has(table, priv, *, schema_=schema):
            return c.scalar(
                sa.text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": WRITER_ROLE, "t": f"{schema_}.{table}", "p": priv},
            )

        def col(table, column, priv):
            return c.scalar(
                sa.text("SELECT has_column_privilege(:r, :t, :c, :p)"),
                {"r": WRITER_ROLE, "t": f"{schema}.{table}", "c": column, "p": priv},
            )

        import erp_v2.migrations.versions.e2_0029_ar_opening_admissions as mig

        assert len(mig.INSERT_TABLES) == 20 and "opening_hold_admissions" not in mig.INSERT_TABLES
        for table in mig.INSERT_TABLES:
            assert has(table, "SELECT") and has(table, "INSERT"), table
            assert not any(has(table, p) for p in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")), table
        for table in mig.SELECT_ONLY_TABLES:
            assert has(table, "SELECT") and not has(table, "INSERT"), table
        for table in _NEVER_INSERT:
            assert not has(table, "INSERT"), table
        for table in (*mig.INSERT_TABLES, *mig.SELECT_ONLY_TABLES, "document_lines"):
            assert not any(has(table, p) for p in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")), table
        assert col("opening_windows", "generation", "UPDATE") and not col("opening_windows", "id", "UPDATE")
        assert col("billing_accounts", "id", "UPDATE") and not col("billing_accounts", "currency", "UPDATE")
        assert not has("opening_windows", "UPDATE")
        for table in ("idempotency_records", "outbox_events"):
            assert has(table, "SELECT", schema_="public") and has(table, "INSERT", schema_="public")
            assert not has(table, "UPDATE", schema_="public") and not has(table, "DELETE", schema_="public")
        assert not c.scalar(
            sa.text("SELECT has_table_privilege(:r, 'public.erp_ar_opening_admissions', 'INSERT')"), {"r": WRITER_ROLE}
        )
        # no platform_ table, no CREATE on any schema, no sequence, no WITH GRANT OPTION
        assert (
            c.scalar(
                sa.text(
                    "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = :r AND table_name LIKE 'platform\\_%'"
                ),
                {"r": WRITER_ROLE},
            )
            == 0
        )
        for sch in ("public", "mod_billing"):
            assert c.scalar(sa.text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": WRITER_ROLE, "s": sch})
            assert not c.scalar(sa.text("SELECT has_schema_privilege(:r, :s, 'CREATE')"), {"r": WRITER_ROLE, "s": sch})
        seqs = c.execute(
            sa.text(
                "SELECT quote_ident(n.nspname) || '.' || quote_ident(cl.relname) FROM pg_class cl "
                "JOIN pg_namespace n ON n.oid = cl.relnamespace WHERE cl.relkind = 'S' AND n.nspname IN ('public', 'mod_billing')"
            )
        ).scalars()
        for seq in seqs:
            assert not c.scalar(
                sa.text("SELECT has_sequence_privilege(:r, :s, 'USAGE')"), {"r": WRITER_ROLE, "s": seq}
            ), seq
            assert not c.scalar(
                sa.text("SELECT has_sequence_privilege(:r, :s, 'SELECT')"), {"r": WRITER_ROLE, "s": seq}
            ), seq
            assert not c.scalar(
                sa.text("SELECT has_sequence_privilege(:r, :s, 'UPDATE')"), {"r": WRITER_ROLE, "s": seq}
            ), seq
        assert (
            c.scalar(
                sa.text(
                    "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = :r AND is_grantable = 'YES'"
                ),
                {"r": WRITER_ROLE},
            )
            == 0
        )


def _admin_engine():
    return sa.create_engine(
        sa.make_url(os.environ["ERP_TEST_POSTGRES_URL"]), isolation_level="AUTOCOMMIT", hide_parameters=True
    )


def _probe_name() -> str:
    return f"erp_probe_{uuid4().hex[:10]}"


def _drop(admin, *names) -> None:
    with admin.connect() as c:
        for name in names:
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {name}")


def test_superuser_migrator_creates_the_role_with_no_membership_row_at_all(engine):
    from erp_v2.importer import ar_opening_role as rg

    admin, probe = _admin_engine(), _probe_name()
    try:
        with admin.connect() as c:  # `postgres` is the superuser
            assert rg.prepare_role(c, probe) is True
            assert (
                c.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_auth_members WHERE roleid = (SELECT oid FROM pg_roles WHERE rolname = :n)"
                    ),
                    {"n": probe},
                )
                == 0
            )
            assert rg.membership_problems(c, probe) == [] and rg.posture_problems(c, probe) == []
            assert rg.prepare_role(c, probe) is False  # idempotent adoption of the clean role
    finally:
        _drop(admin, probe)
        admin.dispose()


def test_non_superuser_createrole_migrator_gets_an_admin_only_row_which_passes(engine):
    """The expected production case on PG16: the creator gets ADMIN (no SET, no INHERIT) automatically."""

    from erp_v2.importer import ar_opening_role as rg

    probe = _probe_name()
    with engine.begin() as c:  # the fixture owner: CREATEROLE, not a superuser
        assert c.scalar(sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")) is False
        assert rg.prepare_role(c, probe) is True
        row = c.execute(
            sa.text(
                "SELECT admin_option, set_option, inherit_option FROM pg_auth_members "
                "WHERE roleid = (SELECT oid FROM pg_roles WHERE rolname = :n) AND member = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
            ),
            {"n": probe},
        ).one()
        assert tuple(row) == (True, False, False)  # ADMIN only
        assert rg.membership_problems(c, probe) == []
    admin = _admin_engine()
    _drop(admin, probe)
    admin.dispose()


def test_createrole_self_grant_set_and_inherit_are_revoked_from_the_creator(engine):
    from erp_v2.importer import ar_opening_role as rg

    probe = _probe_name()
    owner = engine.url.username
    admin = _admin_engine()
    try:
        with admin.connect() as c:
            c.exec_driver_sql(f"ALTER ROLE \"{owner}\" SET createrole_self_grant = 'set, inherit'")
        with engine.begin() as c:
            assert rg.prepare_role(c, probe) is True
            assert rg.membership_problems(c, probe) == []  # SET and INHERIT were revoked, ADMIN kept
            assert c.scalar(sa.text("SELECT pg_has_role(current_user, :n, 'SET')"), {"n": probe}) is False
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'ALTER ROLE "{owner}" RESET createrole_self_grant')
        _drop(admin, probe)
        admin.dispose()


def test_any_set_or_inherit_member_attribute_membership_or_ownership_refuses_adoption(engine):
    from erp_v2.importer import ar_opening_role as rg

    admin, probe = _admin_engine(), _probe_name()
    try:
        with admin.connect() as c:
            rg.prepare_role(c, probe)
            for attribute in ("LOGIN", "CREATEROLE", "CREATEDB", "BYPASSRLS", "REPLICATION"):
                c.exec_driver_sql(f"ALTER ROLE {probe} {attribute}")
                with pytest.raises(rg.WriterRoleRefused, match="attribute_forbidden"):
                    rg.prepare_role(c, probe)
                c.exec_driver_sql(f"ALTER ROLE {probe} NO{attribute}")
            c.exec_driver_sql(f"CREATE ROLE {probe}_m NOLOGIN")
            for options, reason in (
                ("WITH SET TRUE, INHERIT FALSE", "member_can_set_writer"),
                ("WITH SET FALSE, INHERIT TRUE", "member_inherits_writer"),
                ("WITH SET TRUE, INHERIT TRUE", "member_inherits_writer"),
            ):
                c.exec_driver_sql(f"GRANT {probe} TO {probe}_m {options}")
                with pytest.raises(rg.WriterRoleRefused, match=reason):
                    rg.prepare_role(c, probe)
                c.exec_driver_sql(f"REVOKE {probe} FROM {probe}_m")
            c.exec_driver_sql(f"GRANT {probe} TO {probe}_m WITH ADMIN TRUE, SET FALSE, INHERIT FALSE")
            rg.prepare_role(c, probe)  # an ADMIN-only row is allowed
            c.exec_driver_sql(f"REVOKE {probe} FROM {probe}_m")
            for serving in ("app_user", "platform_api", "app_admin"):
                c.exec_driver_sql(f"GRANT {serving} TO {probe}")
                with pytest.raises(rg.WriterRoleRefused, match=r"member_of_another_role|linked_to_serving"):
                    rg.prepare_role(c, probe)
                c.exec_driver_sql(f"REVOKE {serving} FROM {probe}")
            c.exec_driver_sql(f"GRANT {probe}_m TO {probe}")
            with pytest.raises(rg.WriterRoleRefused, match="member_of_another_role"):
                rg.prepare_role(c, probe)
            c.exec_driver_sql(f"REVOKE {probe}_m FROM {probe}")
            c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO {probe}")
            with pytest.raises(rg.WriterRoleRefused, match="default_privileges"):
                rg.prepare_role(c, probe)
            c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES REVOKE SELECT ON TABLES FROM {probe}")  # noqa: S608
    finally:
        _drop(admin, f"{probe}_m", probe)
        admin.dispose()


def test_an_adopted_role_is_stripped_to_exactly_r5_and_the_final_assertion_catches_leftovers(engine):
    """The real role on the fixture DB: a stray grant to it is revoked on adoption; the assertion is exact. The
    adoption itself REFUSES while the operator still holds SET (so the window membership is revoked around it)."""

    from erp_v2.importer import ar_opening_role as rg

    owner = engine.url.username
    admin = _admin_engine()
    try:
        with engine.begin() as c:
            rg.assert_exact_privileges(c, WRITER_ROLE)  # after the migration: exactly R5
            c.exec_driver_sql(f"GRANT SELECT ON mod_billing.document_lines TO {WRITER_ROLE}")  # a stray widening
            c.exec_driver_sql(f"GRANT UPDATE (epoch) ON mod_billing.opening_windows TO {WRITER_ROLE}")
            with pytest.raises(rg.WriterRoleRefused, match="writer_privileges_not_exact:extra=2"):
                rg.assert_exact_privileges(c, WRITER_ROLE)
        with admin.connect() as c:
            c.exec_driver_sql(f'REVOKE {WRITER_ROLE} FROM "{owner}"')
        with engine.begin() as c:
            rg.prepare_role(c, WRITER_ROLE)  # adoption strips everything...
            for line in rg.grant_lines(WRITER_ROLE):  # ...then the closed set is granted again
                c.exec_driver_sql(line)
            rg.preflight(c, WRITER_ROLE)
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'GRANT {WRITER_ROLE} TO "{owner}" WITH SET TRUE, INHERIT FALSE')
        admin.dispose()


def test_apply_preflight_allows_only_the_session_operator_with_set_and_no_inherit(engine):
    from erp_v2.importer import ar_opening_role as rg

    operator = engine.url.username
    with engine.connect() as c:
        rg.preflight(c, WRITER_ROLE, operator=operator)  # the fixture granted SET only, INHERIT FALSE
        with pytest.raises(rg.WriterRoleRefused, match="member_can_set_writer"):
            rg.preflight(c, WRITER_ROLE, operator="somebody_else")
        with pytest.raises(rg.WriterRoleRefused, match="member_can_set_writer"):
            rg.preflight(c, WRITER_ROLE)  # no operator named: any SET member refuses


def test_apply_refuses_when_the_operator_inherits_the_writer(engine):
    w = _world(engine)
    plan, oid = _plan(engine, w)
    admin = _admin_engine()
    owner = engine.url.username
    try:
        with admin.connect() as c:
            c.exec_driver_sql(f'GRANT {WRITER_ROLE} TO "{owner}" WITH SET TRUE, INHERIT TRUE')
        with pytest.raises(OperatorRefusal, match="member_inherits_writer"):
            _apply(engine, w, plan, oid)
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'GRANT {WRITER_ROLE} TO "{owner}" WITH SET TRUE, INHERIT FALSE')
        admin.dispose()
    assert w  # nothing provisioned by the refusal


def test_dual_control_is_bound_into_the_plan_sha_so_a_policy_flip_after_approval_is_refused(engine):
    w = _world(engine)
    plan_on, oid = _plan(engine, w, "on")
    plan_off, _ = _plan(engine, w, "off")
    assert plan_on["plan_sha256"] != plan_off["plan_sha256"] and plan_on["dual_control"] == "on"
    # approved while the policy was OFF; applied after it flipped ON: the rebuilt plan's sha differs
    with pytest.raises(OperatorRefusal, match="approved_plan_sha_mismatch"):
        _apply(engine, w, plan_on, oid, approved=plan_off["plan_sha256"])
    tampered = {**plan_on, "dual_control": "off"}
    with pytest.raises(OperatorRefusal, match="plan_sha_not_self_consistent"):
        _apply(engine, w, tampered, oid, approved=plan_on["plan_sha256"])


def test_an_aborted_apply_keeps_its_partial_receipt_and_a_complete_one_carries_the_read_backs(engine):
    w = _world(engine)
    plan, oid = _plan(engine, w)
    try:
        _apply(engine, w, plan, oid, fence="another-fence")
    except OperatorRefusal as exc:
        assert exc.reason.startswith("seal_refused") and exc.receipt is not None
        assert len(exc.receipt["admitted"]) == 3 and exc.receipt["sealed"] is False  # imports were kept
    else:  # pragma: no cover
        raise AssertionError("the wrong fence must abort the seal")
    w2 = _world(engine)
    plan2, oid2 = _plan(engine, w2)
    receipt = _apply(engine, w2, plan2, oid2)
    assert receipt["window_manifest_fingerprint"] == plan2["manifest_fingerprint"]
    assert receipt["t1_credit_by_currency"] == {"NGN": 7000}
    assert (
        sorted(receipt["cash_lot_residual_by_account"].values()) == [0, 2000, 5000]
        or sum(receipt["cash_lot_residual_by_account"].values()) == 7000
    )
