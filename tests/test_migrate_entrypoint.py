"""Migration entrypoint contract without a live database."""

from __future__ import annotations

import pytest
from alembic.script import ScriptDirectory

from erp_v2.migrate import (
    EXPECTED_HEADS,
    MigrationConfigurationError,
    build_config,
    migration_paths,
    select_url,
)

DSN = "postgresql+psycopg://app_admin:x@127.0.0.1:1/erpv2"


def test_migration_url_has_no_runtime_fallback() -> None:
    with pytest.raises(MigrationConfigurationError):
        select_url(environ={"DATABASE_URL": DSN})
    with pytest.raises(MigrationConfigurationError):
        select_url(environ={"MIGRATION_DATABASE_URL": "sqlite:///tmp/x.db"})


def test_build_config_composes_all_released_lineages() -> None:
    config = build_config(DSN)
    locations = config.get_main_option("version_locations") or ""
    for lineage in (
        "dotmac_kernel",
        "dotmac_accounting",
        "dotmac_billing",
        "dotmac_files",
        "dotmac_inventory",
        "dotmac_payroll",
        "dotmac_people",
        "dotmac_projects",
        "dotmac_tax",
    ):
        assert lineage in locations
    selected = {s.module for s in config.attributes["module_plane_selections"]}
    assert selected == {
        "approvals",
        "billing",
        "collections",
        "durable_timers",
        "files",
        "numbering",
        "ticketing",
    }
    script_location, versions = migration_paths()
    assert (script_location / "env.py").is_file()
    # assembly versions dir + kernel + 31 module lineages
    assert len(versions) == 33
    assert all(path.is_dir() for path in versions)
    heads = ScriptDirectory.from_config(config).get_heads()
    assert sorted(heads) == sorted(EXPECTED_HEADS)
