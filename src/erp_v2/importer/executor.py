"""Transform executor: move rows per spec, exceptions never silent.

Contract (Academy importer rules, `docs/cutover.md` Gate 3):
- The caller owns both connections and the destination transaction;
  this module never commits or rolls back (module convention).
- A row whose FK value has no entry in its id-map is NOT written and
  NOT dropped silently: it is recorded in the exception queue with
  its source key and reason, and the report's `complete` is False.
  An import with unresolved exceptions must not be flipped to.
- `dry_run=True` performs every read and rewrite decision and writes
  nothing; counts in the report are then the rehearsal expectation.
- Identifiers come only from the spec (validated in `spec.py`);
  row values are always bound parameters.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from erp_v2.importer.spec import TransformSpec


@dataclass(frozen=True)
class RowException:
    source_table: str
    source_key: str
    column: str
    reason: str


@dataclass
class TransformReport:
    source_table: str
    destination_table: str
    read: int = 0
    written: int = 0
    exceptions: list[RowException] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.exceptions and self.read == self.written


def execute_transform(
    source_connection: sa.Connection,
    destination_connection: sa.Connection,
    spec: TransformSpec,
    *,
    id_maps: dict[str, dict[Any, Any]] | None = None,
    run_constants: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> TransformReport:
    """Execute one table transform. Raises if the spec is not executable."""

    if not spec.executable:
        raise ValueError(
            f"spec for {spec.destination_table} is not executable: "
            f"missing required columns {list(spec.missing_required)}"
        )
    run_constants = run_constants or {}
    unsupplied = [c for c in spec.run_supplied if c not in run_constants]
    if unsupplied:
        raise ValueError(
            f"spec for {spec.destination_table} needs run-supplied "
            f"values for {unsupplied} (per-tenant-extent run constants)"
        )
    id_maps = id_maps or {}
    report = TransformReport(source_table=spec.source_table, destination_table=spec.destination_table)
    source_columns = list(dict.fromkeys(spec.column_map.values()))
    select_sql = sa.text(
        f"SELECT {', '.join(source_columns)} FROM {spec.source_table}"  # noqa: S608
    )
    dest_columns = (
        list(spec.column_map)
        + [c for c in spec.constants if c not in spec.column_map]
        + [c for c in spec.run_supplied if c not in spec.column_map]
        + [c for c in spec.computed if c not in spec.column_map]
    )
    insert_sql = sa.text(
        # identifiers regex-validated in spec.py; row values are bound params
        f"INSERT INTO {spec.destination_table} ({', '.join(dest_columns)}) "  # noqa: S608
        f"VALUES ({', '.join(f':{c}' for c in dest_columns)})"
    )
    # Identify a stable source key for exception records: the column
    # feeding the destination `id`, else the first mapped column.
    key_source = spec.column_map.get("id", source_columns[0])

    for row in source_connection.execute(select_sql).mappings():
        report.read += 1
        values: dict[str, Any] = {
            **dict(spec.constants),
            **{c: run_constants[c] for c in spec.run_supplied},
        }
        row_exceptions: list[RowException] = []
        for dest_col, source_col in spec.column_map.items():
            value = row[source_col]
            map_name = spec.fk_rewrites.get(dest_col)
            if map_name is not None and value is not None:
                mapping = id_maps.get(map_name, {})
                if value not in mapping:
                    row_exceptions.append(
                        RowException(
                            source_table=spec.source_table,
                            source_key=str(row[key_source]),
                            column=dest_col,
                            reason=f"no {map_name} id-map entry for source value",
                        )
                    )
                    continue
                value = mapping[value]
            values[dest_col] = value
        if row_exceptions:
            report.exceptions.extend(row_exceptions)
            continue
        for dest_col, strategy in spec.computed.items():
            if strategy == "sha256_row":
                digest = hashlib.sha256(repr(sorted((k, str(v)) for k, v in values.items())).encode()).hexdigest()
                values[dest_col] = digest
            elif strategy == "source_key":
                values[dest_col] = f"legacy:{spec.source_table}:{row[key_source]}"
            else:  # pragma: no cover - strategies are closed in spec.py
                raise ValueError(f"unknown computed strategy {strategy!r}")
        if not dry_run:
            destination_connection.execute(insert_sql, values)
            report.written += 1
    if dry_run:
        # Expectation counts: rows that would have been written.
        report.written = report.read - len({e.source_key for e in report.exceptions})
    return report


__all__ = ["RowException", "TransformReport", "execute_transform"]
