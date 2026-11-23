"""F23 on real PostgreSQL: the flip importer refuses a destination that already holds imported data.

Hosted CI only (ERP_TEST_POSTGRES_URL), like the other *_pg suites. Counts are asserted by table NAME only.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

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
    from uuid import UUID

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
