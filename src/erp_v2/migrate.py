"""Migration owner entrypoint for the composed ERP assembly.

Descriptor contract:

    python -m erp_v2.migrate upgrade heads
    python -m erp_v2.migrate current

`MIGRATION_DATABASE_URL` is required. There is deliberately no fallback to
the runtime `DATABASE_URL`: Foundation's credential-separation rule gives
DDL to the migration owner identity alone. Error messages never include the
supplied DSN.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from dotmac_kernel.migrations.configuration import (
    select_migration_database_url,
    set_alembic_database_url,
)
from dotmac_kernel.planes import install_module_plane_selections
from dotmac_kernel.prerequisites import install_prerequisite_bindings

from erp_v2.migration_bindings import (
    ASSEMBLY_MODULE_PLANES,
    ASSEMBLY_PREREQUISITE_BINDINGS,
)


class MigrationConfigurationError(RuntimeError):
    """A sanitized migration configuration refusal."""


# Composed heads at the pinned releases (kernel + 31 modules, waves
# 1-6). The migrate-entrypoint test asserts ScriptDirectory heads equal
# this set; each future pin bump updates its head in the same change.
# Live STAMPED heads after a full genesis are EXPECTED_STAMPED_HEADS:
# e2_0001 depends_on the kernel + people heads, so Alembic absorbs
# their stamps into the e2 lineage (verified live at Gate 4 genesis
# on the dedicated test server, 2026-11-08: 31 stamped, 333 tables).
EXPECTED_HEADS: tuple[str, ...] = (
    "0030_credential_tenant_key",  # kernel 0.1.0a107
    "ac_0001_accounting",  # accounting 0.1.0a1
    "ao_0001_ai_operations",  # ai-operations 0.1.0a1
    "ap_0003_withdrawals",  # approvals 0.1.0a8
    "as_0001_assets",  # assets 0.1.0a1
    "ay_0001_analytics",  # analytics 0.1.0a1
    "bi_0012_tax_evidence",  # billing 0.1.0a10
    "bk_0001_banking",  # banking 0.1.0a1
    "cg_0002_approval_withdrawals",  # commercial-agreements 0.1.0a4
    "cl_0001_collections",  # collections 0.1.0a1
    "cu_0001_customer_accounts",  # customers 0.1.0a1
    "do_0001_documents",  # documents 0.1.0a1
    "e2_0011_leases",  # assembly e2 head (HR + fleet + public sector + leases)
    "dt_0001_durable_timers",  # durable-timers 0.1.0a1
    "ex_0001_expenses",  # expenses 0.1.0a1
    "fi_0002_selectable_planes",  # files 0.1.0a7
    "fm_0001_forms",  # forms 0.2.0a1
    "fn_0001_asset_accounting",  # finance 0.2.0a1
    "ib_0003_transport_refs",  # inbox 0.1.0a3
    "im_0001_import_runs",  # imports 0.2.0a1
    "iv_0001_inventory",  # inventory 0.2.0a1
    "nu_0001_numbering",  # numbering 0.1.0a3
    "pa_0001_payables",  # payables 0.1.0a1
    "pc_0001_procurement",  # procurement 0.1.0a1
    "pe_0001_people_directory",  # people 0.1.0a2
    "pj_0001_projects",  # projects 0.1.0a1
    "pm_0006_cash_qualification",  # payments 0.1.0a3
    "pt_0001_party_context",  # party 0.1.0a1
    "py_0001_payroll",  # payroll 0.1.0a2
    "sa_0001_sales",  # sales 0.1.0a1
    "tk_0001_tickets",  # ticketing 0.1.0a4
    "tx_0005_tax_dependencies",  # tax 0.1.0a7
    "wr_0001_runtime",  # workflow-runtime 0.1.0a1
)

_ABSORBED_HEADS: tuple[str, ...] = (
    "0030_credential_tenant_key",
    "pe_0001_people_directory",
)
EXPECTED_STAMPED_HEADS: tuple[str, ...] = tuple(h for h in EXPECTED_HEADS if h not in _ABSORBED_HEADS)


def _module_dir(package: str) -> Path:
    module_file = import_module(package).__file__
    if module_file is None:  # pragma: no cover - installed packages are files
        raise MigrationConfigurationError(f"installed package {package!r} has no file path")
    return Path(module_file).resolve().parent


def migration_paths() -> tuple[Path, tuple[Path, ...]]:
    """Return `(script_location, version_locations)` for installed lineages."""

    script_location = _module_dir("erp_v2.migrations")
    version_locations = (
        script_location / "versions",
        _module_dir("dotmac_kernel.migrations") / "versions",
        _module_dir("dotmac_accounting.migrations") / "versions",
        _module_dir("dotmac_ai_operations.migrations") / "versions",
        _module_dir("dotmac_analytics.migrations") / "versions",
        _module_dir("dotmac_approvals.migrations") / "versions",
        _module_dir("dotmac_assets.migrations") / "versions",
        _module_dir("dotmac_banking.migrations") / "versions",
        _module_dir("dotmac_billing.migrations") / "versions",
        _module_dir("dotmac_collections.migrations") / "versions",
        _module_dir("dotmac_commercial_agreements.migrations") / "versions",
        _module_dir("dotmac_customers.migrations") / "versions",
        _module_dir("dotmac_documents.migrations") / "versions",
        _module_dir("dotmac_durable_timers.migrations") / "versions",
        _module_dir("dotmac_expenses.migrations") / "versions",
        _module_dir("dotmac_files.migrations") / "versions",
        _module_dir("dotmac_finance.migrations") / "versions",
        _module_dir("dotmac_forms.migrations") / "versions",
        _module_dir("dotmac_imports.migrations") / "versions",
        _module_dir("dotmac_inbox.migrations") / "versions",
        _module_dir("dotmac_inventory.migrations") / "versions",
        _module_dir("dotmac_numbering.migrations") / "versions",
        _module_dir("dotmac_party.migrations") / "versions",
        _module_dir("dotmac_payables.migrations") / "versions",
        _module_dir("dotmac_payments.migrations") / "versions",
        _module_dir("dotmac_payroll.migrations") / "versions",
        _module_dir("dotmac_people.migrations") / "versions",
        _module_dir("dotmac_procurement.migrations") / "versions",
        _module_dir("dotmac_projects.migrations") / "versions",
        _module_dir("dotmac_sales.migrations") / "versions",
        _module_dir("dotmac_tax.migrations") / "versions",
        _module_dir("dotmac_ticketing.migrations") / "versions",
        _module_dir("dotmac_workflow_runtime.migrations") / "versions",
    )
    return script_location, version_locations


def select_url(
    database_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    supplied = database_url if database_url is not None else env.get("MIGRATION_DATABASE_URL")
    try:
        selected = select_migration_database_url(
            migration_database_url=supplied,
            fallback_database_url=None,
        )
        url = sa.make_url(selected)
    except (sa.exc.ArgumentError, ValueError) as exc:
        raise MigrationConfigurationError(
            "MIGRATION_DATABASE_URL is required and must be a valid PostgreSQL DSN"
        ) from exc
    if url.get_dialect().name != "postgresql":
        raise MigrationConfigurationError("MIGRATION_DATABASE_URL is required and must be a valid PostgreSQL DSN")
    return selected


def build_config(database_url: str | None = None) -> Config:
    """Build the Alembic config after installing assembly decisions."""

    selected = select_url(database_url)
    install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)
    install_module_plane_selections(ASSEMBLY_MODULE_PLANES)

    script_location, version_locations = migration_paths()
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("version_locations", os.pathsep.join(str(path) for path in version_locations))
    config.set_main_option("version_path_separator", "os")
    config.set_main_option("erp_v2_migration_url", selected.replace("%", "%%"))
    set_alembic_database_url(config, database_url=selected)
    config.attributes["module_plane_selections"] = ASSEMBLY_MODULE_PLANES
    return config


def _preflight_owner(selected: str) -> None:
    """Refuse a superuser migration identity; name the target database."""

    engine = sa.create_engine(selected, pool_pre_ping=True, hide_parameters=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT current_database(), current_user, "
                    "(SELECT rolsuper FROM pg_catalog.pg_roles "
                    " WHERE rolname = current_user)"
                )
            ).one()
    finally:
        engine.dispose()
    print(f"migration target: database={row[0]} user={row[1]}")
    if row[2]:
        raise MigrationConfigurationError("migration identity is a superuser; use the non-super owner role")


def upgrade_heads(database_url: str | None = None) -> None:
    config = build_config(database_url)
    _preflight_owner(select_url(database_url))
    command.upgrade(config, "heads")


def current_heads(database_url: str | None = None) -> tuple[str, ...]:
    """Return sorted current heads without creating a version table."""

    selected = select_url(database_url)
    engine = sa.create_engine(
        selected,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"application_name": "dotmac-erp-v2-migrate"},
    )
    try:
        with engine.connect() as connection:
            if not sa.inspect(connection).has_table("alembic_version"):
                return ()
            context = MigrationContext.configure(connection)
            return tuple(sorted(context.get_current_heads()))
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.migrate")
    subparsers = parser.add_subparsers(dest="command", required=True)
    upgrade = subparsers.add_parser("upgrade")
    upgrade.add_argument("target", choices=("heads",))
    subparsers.add_parser("current")
    args = parser.parse_args(argv)

    try:
        if args.command == "upgrade":
            upgrade_heads()
        else:
            for head in current_heads():
                print(head)
    except MigrationConfigurationError as exc:
        print(f"migration configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
