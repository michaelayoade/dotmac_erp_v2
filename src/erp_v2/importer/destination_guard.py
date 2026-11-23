"""F23: the flip importer refuses a destination that already holds imported business data.

A repeated ``--apply``, a retry after a partial failure, or an apply against the wrong database must never
double-import. Before any write (same transaction, after the importer's per-tenant advisory lock) the importer counts
rows for the tenant in (a) every importer destination table that carries a ``tenant_id`` column and (b) the
migration-ledger receipt tables. Any row refuses with ``import_destination_not_empty``.

There is deliberately NO override flag: a re-flip starts from a fresh genesis. Table counts are reported by table NAME
only, never row content. Importer target tables WITHOUT a ``tenant_id`` column are counted over the whole table.
The role must be superuser or BYPASSRLS (some targets are FORCE RLS), else ``import_guard_rls_blind``; the
ledger tables must resolve, else ``import_guard_incomplete``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

NOT_EMPTY_CODE = "import_destination_not_empty"
LOCK_HELD_CODE = "import_lock_held"
INCOMPLETE_CODE = "import_guard_incomplete"
RLS_BLIND_CODE = "import_guard_rls_blind"
# Migration-ledger receipt tables (erp_v2.migration_ledger.models): any row means a prior migration run wrote here.
LEDGER_RECEIPT_TABLES = ("erp_migration_exceptions", "erp_bridge_versions", "erp_opening_journals")
_LOCK_NAMESPACE = "erp_v2.importer.execute"


class ImportDestinationNotEmpty(RuntimeError):
    """Named refusal: the destination already holds imported data for the tenant (nothing written)."""

    def __init__(self, counts: Mapping[str, int]):
        self.counts = dict(counts)
        super().__init__(
            f"{NOT_EMPTY_CODE}: destination already holds imported data ("
            + ", ".join(f"{name}={n}" for name, n in sorted(self.counts.items()))
            + "); a re-flip starts from a fresh genesis; nothing written"
        )


class ImportGuardRefused(RuntimeError):
    """Named fail-closed refusal: the guard cannot prove the destination is empty (nothing written)."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}; nothing written")


class ImportLockHeld(RuntimeError):
    """Named refusal: another importer run holds the per-tenant lock."""

    def __init__(self) -> None:
        super().__init__(f"{LOCK_HELD_CODE}: another import run holds the destination lock; nothing written")


@dataclass(frozen=True)
class GuardReport:
    """Read-only finding: table name -> row count for the tenant (non-empty tables only)."""

    counts: dict[str, int] = field(default_factory=dict)
    tables_checked: int = 0
    checked_names: tuple[str, ...] = ()

    @property
    def not_empty(self) -> bool:
        return bool(self.counts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": NOT_EMPTY_CODE if self.not_empty else None,
            "not_empty": self.not_empty,
            "tables_checked": self.tables_checked,
            "tables_checked_names": list(self.checked_names),
            "table_row_counts": dict(sorted(self.counts.items())),
        }

    def raise_if_not_empty(self) -> None:
        if self.not_empty:
            raise ImportDestinationNotEmpty(self.counts)


def take_import_lock(connection: sa.Connection, tenant_hex: str | None) -> None:
    """Session-level advisory lock held for the whole run (apply commits per spec, so xact locks would drop).

    Released when the connection closes. Non-blocking: a concurrent run is refused, never queued.
    """

    if connection.dialect.name != "postgresql":  # the real destination is always PostgreSQL; sqlite test harnesses only
        return
    key = f"{_LOCK_NAMESPACE}:{tenant_hex or '*'}"
    got = connection.execute(sa.text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key}).scalar()
    if not got:
        raise ImportLockHeld


def release_import_lock(connection: sa.Connection, tenant_hex: str | None) -> None:
    """Explicit release (tests / pooled callers); the CLI instead relies on closing its un-pooled connection."""

    if connection.dialect.name != "postgresql":
        return
    key = f"{_LOCK_NAMESPACE}:{tenant_hex or '*'}"
    connection.execute(sa.text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key})


def _tenant_value(column: sa.Column, tenant_hex: str) -> Any:
    return uuid.UUID(tenant_hex) if isinstance(column.type, sa.Uuid) else tenant_hex


def _ledger_tables() -> dict[str, sa.Table]:
    """The three ledger receipt tables taken directly from their models (never via the CLI's metadata imports)."""

    from erp_v2.migration_ledger.models import BridgeVersion, MigrationException, OpeningJournal

    tables = {t.__table__.name: t.__table__ for t in (MigrationException, BridgeVersion, OpeningJournal)}
    if set(tables) != set(LEDGER_RECEIPT_TABLES) or any("tenant_id" not in t.c for t in tables.values()):
        raise ImportGuardRefused(INCOMPLETE_CODE, "ledger receipt tables unresolved")
    return {t.key: t for t in tables.values()}


def _require_rls_visibility(connection: sa.Connection) -> None:
    """Some target tables are FORCE ROW LEVEL SECURITY: a role that cannot bypass RLS would count 0 and fail open."""

    ok = connection.execute(
        sa.text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar()
    if not ok:
        raise ImportGuardRefused(RLS_BLIND_CODE, "the counting role is neither superuser nor BYPASSRLS")


def inspect_destination(
    connection: sa.Connection,
    specs: Sequence[Any],
    destination_tables: Mapping[str, sa.Table],
    tenant_hex: str | None,
) -> GuardReport:
    """Count rows in the importer's target tables and the ledger receipt tables. Read-only; fails closed.

    Tables with a ``tenant_id`` are counted for the tenant; spec destinations WITHOUT one are counted whole-table.
    """

    postgres = connection.dialect.name == "postgresql"
    if postgres:
        _require_rls_visibility(connection)
    scoped: dict[str, sa.Table] = {}
    for spec in specs:
        if spec.source_row_refusal is None and spec.destination_table in destination_tables:
            scoped[spec.destination_table] = destination_tables[spec.destination_table]
    ledger = _ledger_tables()
    scoped.update(ledger)
    counts: dict[str, int] = {}
    checked: list[str] = []
    inspector = sa.inspect(connection)
    for key in sorted(scoped):
        table = scoped[key]
        exists = inspector.has_table(table.name, schema=table.schema)
        if not exists:
            if postgres and key in ledger:
                raise ImportGuardRefused(INCOMPLETE_CODE, f"ledger receipt table {key} is missing on the destination")
            continue  # not yet created: no rows can exist
        checked.append(key)
        query = sa.select(sa.func.count()).select_from(table)
        if "tenant_id" in table.c and tenant_hex is not None:
            column = table.c.tenant_id
            query = query.where(column == _tenant_value(column, tenant_hex))
        n = int(connection.execute(query).scalar() or 0)
        if n:
            counts[key] = n
    if postgres and not {t.key for t in ledger.values()} <= set(checked):
        raise ImportGuardRefused(INCOMPLETE_CODE, "ledger receipt tables not all checked")
    return GuardReport(counts=counts, tables_checked=len(checked), checked_names=tuple(checked))


__all__ = [
    "INCOMPLETE_CODE",
    "LEDGER_RECEIPT_TABLES",
    "LOCK_HELD_CODE",
    "NOT_EMPTY_CODE",
    "RLS_BLIND_CODE",
    "GuardReport",
    "ImportDestinationNotEmpty",
    "ImportGuardRefused",
    "ImportLockHeld",
    "inspect_destination",
    "release_import_lock",
    "take_import_lock",
]
