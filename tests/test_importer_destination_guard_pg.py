"""F23 on real PostgreSQL: the flip importer refuses a destination that already holds imported data.

Hosted CI only (ERP_TEST_POSTGRES_URL), like the other *_pg suites. Counts are asserted by table NAME only.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting.models import AccountCategory, AccountClass
from dotmac_kernel.models import Tenant
from sqlalchemy.orm import Session
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.importer import cli
from erp_v2.importer.destination_guard import NOT_EMPTY_CODE, inspect_destination, release_import_lock
from erp_v2.migration_ledger.models import MigrationException

CATEGORY_KEY = AccountCategory.__table__.key
LEDGER_KEY = MigrationException.__table__.key
SPEC = SimpleNamespace(source_row_refusal=None, destination_table=CATEGORY_KEY)
MANIFEST = {
    "snapshot_sha256": "a" * 64,
    "source_database": "legacy_snapshot_20261108",
    "destination_database": "erp_v2_candidate",
    "candidate_revision": "deadbeef",
}


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


def _tenant(engine) -> str:
    tenant_id = uuid4()
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, slug=f"t{tenant_id.hex[:24]}", name="F23"))
        db.commit()
    return tenant_id.hex


def _add_category(engine, tenant_hex: str) -> None:
    with Session(engine) as db:
        db.add(AccountCategory(tenant_id=UUID(tenant_hex), code="AST", name="AST", account_class=AccountClass.ASSET))
        db.commit()


def _add_receipt(engine, tenant_hex: str) -> None:
    from uuid import UUID

    with Session(engine) as db:
        db.add(
            MigrationException(
                tenant_id=UUID(tenant_hex),
                class_code="X",
                legacy_key="k",
                source_kind="t",
                state="open",
                fingerprint="f" * 64,
                source_receipt_sha256="a" * 64,
                detail={},
            )
        )
        db.commit()


def _count(engine, key: str, tenant_hex: str) -> int:
    from uuid import UUID

    table = cli._destination_tables()[key]
    with engine.connect() as c:
        return int(c.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.tenant_id == UUID(tenant_hex))))


def _guard(engine, tenant_hex: str, *, enforce: bool):
    with engine.connect() as conn:
        try:
            return cli._guard_destination(conn, [SPEC], cli._destination_tables(), tenant_hex, enforce=enforce)
        finally:
            conn.rollback()
            release_import_lock(conn, tenant_hex)
            conn.commit()


def test_empty_destination_proceeds(engine) -> None:
    tenant = _tenant(engine)
    report = _guard(engine, tenant, enforce=True)
    assert not report.not_empty and report.tables_checked >= 2


def test_non_empty_destination_refused_with_zero_writes(engine) -> None:
    tenant = _tenant(engine)
    _add_category(engine, tenant)
    before = _count(engine, CATEGORY_KEY, tenant)
    with pytest.raises(cli.CliRefusal) as err:
        _guard(engine, tenant, enforce=True)
    assert NOT_EMPTY_CODE in str(err.value) and f"{CATEGORY_KEY}=1" in str(err.value)
    assert "nothing written" in str(err.value)
    assert _count(engine, CATEGORY_KEY, tenant) == before == 1


def test_ledger_receipt_alone_refuses(engine) -> None:
    tenant = _tenant(engine)
    _add_receipt(engine, tenant)
    with pytest.raises(cli.CliRefusal, match=f"{NOT_EMPTY_CODE}.*{LEDGER_KEY}=1"):
        _guard(engine, tenant, enforce=True)


def test_other_tenant_rows_do_not_count(engine) -> None:
    other, mine = _tenant(engine), _tenant(engine)
    _add_category(engine, other)
    _add_receipt(engine, other)
    assert not _guard(engine, mine, enforce=True).not_empty


def test_dry_run_reports_without_refusing(engine) -> None:
    tenant = _tenant(engine)
    _add_category(engine, tenant)
    report = _guard(engine, tenant, enforce=False)
    assert report.as_dict()["code"] == NOT_EMPTY_CODE
    assert report.counts == {CATEGORY_KEY: 1}


def test_concurrent_run_refused_by_lock(engine) -> None:
    tenant = _tenant(engine)
    with engine.connect() as first, engine.connect() as second:
        cli._guard_destination(first, [SPEC], cli._destination_tables(), tenant, enforce=True)
        with pytest.raises(cli.CliRefusal, match="import_lock_held"):
            cli._guard_destination(second, [SPEC], cli._destination_tables(), tenant, enforce=True)
        first.rollback()
        release_import_lock(first, tenant)
        first.commit()
    # the session lock is released with its connection
    assert not _guard(engine, tenant, enforce=True).not_empty


def test_no_tenant_filter_counts_whole_table(engine) -> None:
    tenant = _tenant(engine)
    _add_category(engine, tenant)
    with engine.connect() as conn:
        report = inspect_destination(conn, [SPEC], cli._destination_tables(), None)
        conn.rollback()
    assert report.counts[CATEGORY_KEY] >= 1


@pytest.mark.parametrize("populated", [False, True])
def test_plan_reports_condition(engine, tmp_path, monkeypatch, populated) -> None:
    tenant = _tenant(engine)
    if populated:
        _add_receipt(engine, tenant)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({**MANIFEST, "tenant_extent": [tenant]}))
    receipt = tmp_path / "r.json"
    monkeypatch.delenv("LEGACY_DATABASE_URL", raising=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    assert cli.main(["plan", "--manifest", str(manifest), "--receipt", str(receipt)]) == 0
    guard = json.loads(receipt.read_text())["destination_guard"]
    assert guard["checked"] is True and guard["not_empty"] is populated
    assert (guard["code"] == NOT_EMPTY_CODE) is populated
    assert (LEDGER_KEY in guard["table_row_counts"]) is populated


# ---- review fixes: production path (H1), RLS (M1), whole-table spec destinations (M2), end-to-end execute ----


AGREEMENTS_KEY = "mod_agreements.agreements"


def _manifest_file(tmp_path, tenant_hex: str) -> Path:
    path = tmp_path / "m.json"
    path.write_text(json.dumps({**MANIFEST, "tenant_extent": [tenant_hex]}))
    return path


def _dsn(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_real_cli_plan_in_fresh_interpreter_counts_ledger_tables(engine, tmp_path) -> None:
    """H1: no test-side model imports. The real entry point must itself resolve and count the three ledger tables."""
    tenant = _tenant(engine)
    _add_receipt(engine, tenant)
    receipt = tmp_path / "r.json"
    env = {k: v for k, v in os.environ.items() if k not in ("LEGACY_DATABASE_URL", "DATABASE_URL")}
    env["MIGRATION_DATABASE_URL"] = _dsn(engine)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(cli.__file__).parents[2]), env.get("PYTHONPATH", "")])
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "erp_v2.importer",
            "plan",
            "--manifest",
            str(_manifest_file(tmp_path, tenant)),
            "--receipt",
            str(receipt),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    guard = json.loads(receipt.read_text())["destination_guard"]
    assert guard["checked"] is True and guard["not_empty"] is True
    assert guard["table_row_counts"] == {LEDGER_KEY: 1}
    for name in ("erp_migration_exceptions", "erp_bridge_versions", "erp_opening_journals"):
        assert name in guard["tables_checked_names"]
    assert guard["tables_checked"] > 3  # pairing destinations are counted too, never "checked with 0 tables"


def test_fresh_genesis_is_empty_for_every_pairing_destination(engine, tmp_path, monkeypatch) -> None:
    """M2 safety: whole-table counting of tenant-less targets must not trip on genesis seeds."""
    tenant = _tenant(engine)
    receipt = tmp_path / "r.json"
    monkeypatch.delenv("LEGACY_DATABASE_URL", raising=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _dsn(engine))
    assert cli.main(["plan", "--manifest", str(_manifest_file(tmp_path, tenant)), "--receipt", str(receipt)]) == 0
    guard = json.loads(receipt.read_text())["destination_guard"]
    assert guard["not_empty"] is False and AGREEMENTS_KEY in guard["tables_checked_names"]


def test_tenantless_spec_destination_counted_whole_table(engine) -> None:
    tables = cli._destination_tables()
    agreements = tables[AGREEMENTS_KEY]
    assert "tenant_id" not in agreements.c
    spec = SimpleNamespace(source_row_refusal=None, destination_table=AGREEMENTS_KEY)
    tenant = _tenant(engine)
    with engine.begin() as c:
        c.execute(
            agreements.insert().values(
                reference=f"r{uuid4().hex[:12]}",
                agreement_family_id=uuid4().hex,
                counterparty_ref="c",
                agreement_type="t",
                effective_date="2026-01-01",
                expiry_date="2026-12-31",
            )
        )
    with engine.connect() as conn:
        report = inspect_destination(conn, [spec], tables, tenant)
        conn.rollback()
    assert report.counts[AGREEMENTS_KEY] >= 1


def test_rls_blind_role_refused(engine) -> None:
    """M1: a role that is neither superuser nor BYPASSRLS would count 0 on FORCE RLS tables; refuse."""
    role = f"f23_nb_{uuid4().hex[:10]}"
    owner = engine.url.username
    with engine.connect() as conn:
        conn.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN NOBYPASSRLS')
        conn.exec_driver_sql(f'GRANT "{role}" TO "{owner}"')
        conn.commit()
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f'SET ROLE "{role}"')
            with pytest.raises(cli.CliRefusal, match="import_guard_rls_blind"):
                cli._guard_destination(conn, [SPEC], cli._destination_tables(), _tenant(engine), enforce=True)
            conn.rollback()
    finally:
        with engine.connect() as conn:
            conn.exec_driver_sql(f'DROP ROLE "{role}"')
            conn.commit()


def test_missing_ledger_table_on_postgres_refused(engine) -> None:
    """H1: the ledger tables absent from the destination is a named refusal, never silently green."""
    tenant = _tenant(engine)
    tables = dict(cli._destination_tables())
    ghost = sa.Table("erp_opening_journals", sa.MetaData(), sa.Column("tenant_id", sa.Uuid()), schema="f23_ghost")
    assert ghost.schema == "f23_ghost"
    with engine.connect() as conn:
        from erp_v2.importer import destination_guard as dg

        original = dg._ledger_tables
        try:
            dg._ledger_tables = lambda: {"f23_ghost.erp_opening_journals": ghost}
            with pytest.raises(dg.ImportGuardRefused, match="import_guard_incomplete"):
                inspect_destination(conn, [], tables, tenant)
        finally:
            dg._ledger_tables = original
        conn.rollback()


def _execute_env(monkeypatch, tmp_path, engine, tenant):
    src = tmp_path / "src.db"
    sa.create_engine(f"sqlite:///{src}").dispose()
    monkeypatch.setattr(cli, "_resolve_sources", lambda e, p: (set(), {}, {}, ()))
    monkeypatch.setattr(cli, "build_specs", lambda present, cols, dest: ((), ()))
    monkeypatch.setenv("ERP_IMPORT_APPROVED", "1")
    monkeypatch.setenv("ERP_IMPORT_APPLY_OK", "1")
    monkeypatch.setenv("LEGACY_DATABASE_URL", f"sqlite:///{src}")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _dsn(engine))
    return ["--manifest", str(_manifest_file(tmp_path, tenant)), "--receipt", str(tmp_path / "r.json")]


def _no_enrolment(monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("EnrolmentMaps (and so the pre-mints) must not run after a refusal")

    monkeypatch.setattr(cli, "EnrolmentMaps", boom)


def _total(engine, name: str) -> int:
    with engine.connect() as c:
        return int(c.scalar(sa.text(f"SELECT count(*) FROM {name}")))  # noqa: S608


def test_execute_apply_non_empty_refused_before_pre_mints(engine, tmp_path, monkeypatch) -> None:
    tenant = _tenant(engine)
    _add_receipt(engine, tenant)
    base = _execute_env(monkeypatch, tmp_path, engine, tenant)
    _no_enrolment(monkeypatch)
    before = {t: _total(engine, t) for t in ("tenants", "domain_settings", LEDGER_KEY)}
    assert cli.main(["execute", *base, "--apply", "--mint"]) == 2
    assert not (tmp_path / "r.json").exists()
    assert {t: _total(engine, t) for t in before} == before


def test_execute_dry_run_non_empty_exits_3_with_complete_false(engine, tmp_path, monkeypatch) -> None:
    tenant = _tenant(engine)
    _add_receipt(engine, tenant)
    base = _execute_env(monkeypatch, tmp_path, engine, tenant)
    assert cli.main(["execute", *base]) == 3
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["complete"] is False and data["dry_run"] is True
    assert data["destination_guard"]["code"] == NOT_EMPTY_CODE


def test_execute_empty_destination_apply_proceeds_and_lock_released(engine, tmp_path, monkeypatch) -> None:
    tenant = _tenant(engine)
    base = _execute_env(monkeypatch, tmp_path, engine, tenant)
    seen = []
    real = cli.sa.create_engine
    monkeypatch.setattr(cli.sa, "create_engine", lambda url, **kw: (seen.append((str(url), kw)), real(url, **kw))[1])
    assert cli.main(["execute", *base, "--apply"]) == 0
    from sqlalchemy.pool import NullPool

    assert any(kw.get("poolclass") is NullPool for url, kw in seen if url.startswith("postgresql"))
    assert json.loads((tmp_path / "r.json").read_text())["destination_guard"]["not_empty"] is False
    # the session lock died with the connection: another run can take it at once
    with engine.connect() as conn:
        from erp_v2.importer.destination_guard import take_import_lock

        take_import_lock(conn, UUID(tenant).hex)
        release_import_lock(conn, UUID(tenant).hex)
        conn.commit()
