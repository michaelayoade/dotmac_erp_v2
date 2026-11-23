"""F23: the flip importer refuses a destination that already holds imported business data.

A repeated ``--apply``, a retry after a partial failure, or an apply against the wrong database must never
double-import. Before any write (same transaction, after the importer's per-tenant advisory lock) the importer counts
rows for the tenant in (a) every importer destination table that carries a ``tenant_id`` column and (b) the
migration-ledger receipt tables. Any row refuses with ``import_destination_not_empty``.

There is deliberately NO override flag: a re-flip starts from a fresh genesis. Table counts are reported by table NAME
only, never row content. Tables without a ``tenant_id`` column (global lookups, genesis seeds) are not inspected.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from erp_v2.importer.spec import TransformSpec

NOT_EMPTY_CODE = "import_destination_not_empty"
LOCK_HELD_CODE = "import_lock_held"
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


class ImportLockHeld(RuntimeError):
    """Named refusal: another importer run holds the per-tenant lock."""

    def __init__(self) -> None:
        super().__init__(f"{LOCK_HELD_CODE}: another import run holds the destination lock; nothing written")


@dataclass(frozen=True)
class GuardReport:
    """Read-only finding: table name -> row count for the tenant (non-empty tables only)."""

    counts: dict[str, int] = field(default_factory=dict)
    tables_checked: int = 0

    @property
    def not_empty(self) -> bool:
        return bool(self.counts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": NOT_EMPTY_CODE if self.not_empty else None,
            "not_empty": self.not_empty,
            "tables_checked": self.tables_checked,
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


def inspect_destination(
    connection: sa.Connection,
    specs: Sequence[TransformSpec],
    destination_tables: Mapping[str, sa.Table],
    tenant_hex: str | None,
) -> GuardReport:
    """Count tenant rows in the importer's target tables and the ledger receipt tables. Read-only."""

    names: dict[str, sa.Table] = {}
    for spec in specs:
        if spec.source_row_refusal is None and spec.destination_table in destination_tables:
            names[spec.destination_table] = destination_tables[spec.destination_table]
    for key, table in destination_tables.items():
        if table.name in LEDGER_RECEIPT_TABLES:
            names[key] = table
    counts: dict[str, int] = {}
    checked = 0
    inspector = sa.inspect(connection)
    for key in sorted(names):
        table = names[key]
        if "tenant_id" not in table.c or not inspector.has_table(table.name, schema=table.schema):
            continue  # no tenant scope, or not yet created (no rows can exist)
        checked += 1
        query = sa.select(sa.func.count()).select_from(table)
        if tenant_hex is not None:
            column = table.c.tenant_id
            query = query.where(column == _tenant_value(column, tenant_hex))
        n = int(connection.execute(query).scalar() or 0)
        if n:
            counts[key] = n
    return GuardReport(counts=counts, tables_checked=checked)


__all__ = [
    "LEDGER_RECEIPT_TABLES",
    "LOCK_HELD_CODE",
    "NOT_EMPTY_CODE",
    "GuardReport",
    "ImportDestinationNotEmpty",
    "ImportLockHeld",
    "inspect_destination",
    "release_import_lock",
    "take_import_lock",
]
