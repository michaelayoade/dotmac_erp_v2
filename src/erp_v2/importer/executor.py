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
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from erp_v2.importer.spec import TransformSpec

_MISSING = object()

_BATCH_SIZE = 500


def _flush_batch(
    destination_connection: sa.Connection,
    insert_sql: sa.TextClause,
    batch: list[tuple[str, dict[str, Any]]],
    spec: TransformSpec,
    report: TransformReport,
    fk_retry: list[tuple[str, dict[str, Any]]] | None = None,
) -> None:
    """Insert one batch under a savepoint; binary-split on rejection.

    Batched executemany is the fast path (a per-row savepoint for
    every row made the Gate 4 apply untenable on ~478k journal
    lines). A rejected batch splits recursively until single rows,
    so each bad row still becomes its own named exception and good
    rows still land - at log-scale savepoint cost, not per-row.

    Single rows rejected for a foreign key go to `fk_retry` (when
    given) instead of the exception queue: their parent may land
    later in the same spec (self-FK trees like departments) - the
    executor retries them once after the main pass.
    """

    try:
        with destination_connection.begin_nested():
            destination_connection.execute(insert_sql, [params for _key, params in batch])
        report.written += len(batch)
        return
    except sa.exc.IntegrityError as exc:
        if len(batch) == 1:
            orig_name = type(exc.orig).__name__ if exc.orig is not None else ""
            if fk_retry is not None and ("ForeignKey" in orig_name or "FOREIGN KEY" in str(exc.orig).upper()):
                fk_retry.append(batch[0])
                return
            detail = str(exc.orig).splitlines()[0][:160] if exc.orig is not None else ""
            report.exceptions.append(
                RowException(
                    source_table=spec.source_table,
                    source_key=batch[0][0],
                    column="*",
                    reason=f"destination rejected row ({type(exc.orig).__name__}): {detail}",
                )
            )
            return
    midpoint = len(batch) // 2
    _flush_batch(destination_connection, insert_sql, batch[:midpoint], spec, report, fk_retry)
    _flush_batch(destination_connection, insert_sql, batch[midpoint:], spec, report, fk_retry)


def _map_get(mapping: dict, value: object) -> object:
    """Map lookup tolerant of UUID spellings (object / dashed / hex).

    Enrolment maps normalize UUID keys to 32-char hex (the module
    CHAR(32) id form); legacy source rows may carry uuid.UUID
    objects or dashed strings for the same id.
    """

    found = mapping.get(value, _MISSING)
    if found is not _MISSING:
        return found
    candidate: str | None = None
    if isinstance(value, uuid.UUID):
        candidate = value.hex
    elif isinstance(value, str) and len(value) == 36 and value.count("-") == 4:
        candidate = value.replace("-", "")
    if candidate is not None:
        return mapping.get(candidate, _MISSING)
    return _MISSING


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
    dest_columns = list(
        dict.fromkeys(
            list(spec.column_map)
            + list(spec.constants)
            + list(spec.defaults)
            + list(spec.run_supplied)
            + list(spec.computed)
            + list(spec.lookups)
        )
    )
    insert_sql = sa.text(
        # identifiers regex-validated in spec.py; row values are bound params
        f"INSERT INTO {spec.destination_table} ({', '.join(dest_columns)}) "  # noqa: S608
        f"VALUES ({', '.join(f':{c}' for c in dest_columns)})"
    )
    # Identify a stable source key for exception records: the column
    # feeding the destination `id`, else the first mapped column.
    key_source = spec.column_map.get("id", source_columns[0])
    batch: list[tuple[str, dict[str, Any]]] = []
    fk_retry: list[tuple[str, dict[str, Any]]] = []

    for row in source_connection.execute(select_sql).mappings():
        report.read += 1
        values: dict[str, Any] = {
            **dict(spec.defaults),
            **dict(spec.constants),
            **{c: run_constants[c] for c in spec.run_supplied},
        }
        row_exceptions: list[RowException] = []
        for dest_col, source_col in spec.column_map.items():
            value = row[source_col]
            map_name = spec.fk_rewrites.get(dest_col)
            if map_name is not None and value is not None:
                rewritten = _map_get(id_maps.get(map_name, {}), value)
                if rewritten is _MISSING:
                    row_exceptions.append(
                        RowException(
                            source_table=spec.source_table,
                            source_key=str(row[key_source]),
                            column=dest_col,
                            reason=f"no {map_name} id-map entry for source value",
                        )
                    )
                    continue
                value = rewritten
            if value is None and dest_col in spec.null_fallbacks:
                strategy = spec.null_fallbacks[dest_col]
                if strategy == "row_id":
                    value = str(row[key_source])
                elif strategy == "source_key":
                    value = f"legacy:{spec.source_table}:{row[key_source]}"
                elif strategy.startswith("literal:"):
                    value = strategy.removeprefix("literal:")
                elif strategy.startswith("run:"):
                    value = run_constants.get(strategy.removeprefix("run:"))
            values[dest_col] = value
        # Lookup columns read from caller-supplied maps keyed by a
        # mapped destination value (post-rewrite). No entry, or an
        # entry without the ruled field, is an exception.
        for dest_col, (map_name, key_col, lookup_field) in spec.lookups.items():
            if dest_col in values:
                continue
            entry = _map_get(id_maps.get(map_name, {}), values.get(key_col))
            if entry is _MISSING or entry is None or lookup_field not in entry:
                row_exceptions.append(
                    RowException(
                        source_table=spec.source_table,
                        source_key=str(row[key_source]),
                        column=dest_col,
                        reason=f"no {map_name} lookup entry field {lookup_field!r} for {key_col} value",
                    )
                )
                continue
            values[dest_col] = entry[lookup_field]
        if row_exceptions:
            report.exceptions.extend(row_exceptions)
            continue
        computed_exceptions: list[RowException] = []
        for dest_col, strategy in spec.computed.items():
            if strategy == "sha256_row":
                digest = hashlib.sha256(repr(sorted((k, str(v)) for k, v in values.items())).encode()).hexdigest()
                values[dest_col] = digest
            elif strategy == "source_key":
                values[dest_col] = f"legacy:{spec.source_table}:{row[key_source]}"
            elif strategy == "uuid5_row":
                seed = f"{spec.source_table}:" + repr(sorted((k, str(v)) for k, v in values.items()))
                values[dest_col] = uuid.uuid5(uuid.NAMESPACE_URL, seed).hex
            elif strategy.startswith("mul:"):
                factor_a, factor_b = strategy.removeprefix("mul:").split(":")
                raw_a, raw_b = values.get(factor_a), values.get(factor_b)
                try:
                    product = Decimal(str(raw_a)) * Decimal(str(raw_b))
                except (ArithmeticError, ValueError, TypeError):
                    computed_exceptions.append(
                        RowException(
                            source_table=spec.source_table,
                            source_key=str(row[key_source]),
                            column=dest_col,
                            reason=f"mul factors {factor_a!r}/{factor_b!r} are not numeric in the source row",
                        )
                    )
                    continue
                values[dest_col] = product
            elif strategy.startswith("slug:"):
                raw = values.get(strategy.removeprefix("slug:"))
                slug = re.sub(r"[^a-z0-9]+", "-", str(raw or "").lower()).strip("-")[:80].rstrip("-")
                if not slug:
                    computed_exceptions.append(
                        RowException(
                            source_table=spec.source_table,
                            source_key=str(row[key_source]),
                            column=dest_col,
                            reason=f"slug source {strategy!r} produced an empty key",
                        )
                    )
                    continue
                values[dest_col] = slug
            else:  # pragma: no cover - strategies are closed in spec.py
                raise ValueError(f"unknown computed strategy {strategy!r}")
        if computed_exceptions:
            report.exceptions.extend(computed_exceptions)
            continue
        # Declared convention: a legacy row never updated carries a
        # NULL updated_at; module TimestampMixin columns are NOT NULL
        # with only a Python onupdate default. Coalesce to created_at
        # (the row's own evidence), never to import time.
        if values.get("updated_at") is None and values.get("created_at") is not None:
            values["updated_at"] = values["created_at"]
        if not dry_run:
            params = values
            if destination_connection.dialect.name == "sqlite":
                # sqlite3 cannot bind Decimal; NUMERIC affinity stores
                # the float. Postgres (the rehearsal target) binds
                # Decimal natively and keeps exactness.
                params = {k: float(v) if isinstance(v, Decimal) else v for k, v in values.items()}
            batch.append((str(row[key_source]), params))
            if len(batch) >= _BATCH_SIZE:
                _flush_batch(destination_connection, insert_sql, batch, spec, report, fk_retry)
                batch = []
    if batch:
        _flush_batch(destination_connection, insert_sql, batch, spec, report, fk_retry)
    # One retry pass for FK-rejected rows: parents landing later in
    # this same spec (self-FK trees) now satisfy them. Rows still
    # rejected become ordinary named exceptions (fk_retry=None).
    for start in range(0, len(fk_retry), _BATCH_SIZE):
        _flush_batch(
            destination_connection,
            insert_sql,
            fk_retry[start : start + _BATCH_SIZE],
            spec,
            report,
        )
    if dry_run:
        # Expectation counts: rows that would have been written.
        report.written = report.read - len({e.source_key for e in report.exceptions})
    return report


__all__ = ["RowException", "TransformReport", "execute_transform"]
