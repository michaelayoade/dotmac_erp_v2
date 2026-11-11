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
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from erp_v2.importer.spec import TransformSpec
from erp_v2.importer.sub_origin import (
    SUB_CUSTOMER_REFERENCE_REASON,
    SUB_ORIGIN_COLUMN,
    SubOriginPolicy,
    norm_ref,
    tagged_customer_reference_sql,
)

_MISSING = object()

_BATCH_SIZE = 500


@dataclass(frozen=True)
class _FkGuard:
    """One destination FK pre-validated against preloaded parents."""

    constraint: str
    local_columns: tuple[str, ...]
    referred_table: str
    referred_columns: tuple[str, ...]
    parents: frozenset[tuple[Any, ...]]


def _fk_guards(
    destination_connection: sa.Connection,
    spec: TransformSpec,
    insert_columns: Sequence[str],
) -> tuple[_FkGuard, ...]:
    """Preload destination FK parent keys for identity FK columns.

    Gate 4 live finding: discovering missing parents via insert
    failures costs a binary-split storm (59k bank lines whose
    statements never landed burned 30+ minutes in savepoint
    churn). Preloading referred keys once per spec turns those
    rows into immediate, identically-named exceptions. Deferred
    columns and columns with id-map rewrites are excluded (their
    values are not destination keys yet / not inserted); the DB
    remains the final arbiter for everything else.
    """

    if "." in spec.destination_table:
        schema, table = spec.destination_table.split(".", 1)
    else:
        schema, table = None, spec.destination_table
    try:
        inspector = sa.inspect(destination_connection)
        foreign_keys = inspector.get_foreign_keys(table, schema=schema)
    except Exception:  # reflection unavailable: DB-side checks only
        return ()
    guards: list[_FkGuard] = []
    insert_set = set(insert_columns)
    for fk in foreign_keys:
        local = tuple(fk.get("constrained_columns") or ())
        referred_cols = tuple(fk.get("referred_columns") or ())
        if not local or len(local) != len(referred_cols):
            continue
        if not set(local) <= insert_set:
            continue
        if any(c in spec.deferred or c in spec.fk_rewrites for c in local):
            continue
        # referred_schema=None means the DB default schema, NOT
        # this table's schema (kernel `tenants` lives in public
        # while referencing tables live in mod_* schemas) - keep
        # the bare name and let search_path resolve it.
        referred_schema = fk.get("referred_schema")
        referred_table = fk.get("referred_table") or ""
        # Self-referential FKs stay DB-arbitrated: parents land
        # later in the same spec (FK retry / deferred columns).
        if referred_table == table and referred_schema in (schema, None):
            continue
        qualified = f"{referred_schema}.{referred_table}" if referred_schema else referred_table
        cols_sql = ", ".join(referred_cols)
        try:
            with destination_connection.begin_nested():
                rows = destination_connection.execute(
                    sa.text(f"SELECT {cols_sql} FROM {qualified}")  # noqa: S608
                ).all()
        except sa.exc.SQLAlchemyError:
            continue  # unresolvable parent: DB-side checks only
        parents = frozenset(tuple(_norm_key(v) for v in row) for row in rows)
        guards.append(
            _FkGuard(
                constraint=fk.get("name") or "foreign_key",
                local_columns=local,
                referred_table=qualified,
                referred_columns=referred_cols,
                parents=parents,
            )
        )
    return tuple(guards)


def _norm_key(value: Any) -> Any:
    """UUID -> hex, matching how mapped values may be spelled."""

    return value.hex if isinstance(value, uuid.UUID) else value


def _guard_key(values: dict[str, Any], columns: tuple[str, ...]) -> tuple[Any, ...] | None:
    key: list[Any] = []
    for col in columns:
        value = values.get(col)
        if value is None:
            return None  # MATCH SIMPLE: any NULL passes the FK
        if isinstance(value, uuid.UUID):
            value = value.hex
        elif isinstance(value, str) and len(value) == 36 and value.count("-") == 4:
            value = value.replace("-", "")
        key.append(value)
    return tuple(key)


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
    except (sa.exc.IntegrityError, sa.exc.DataError) as exc:
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
    source_error: str | None = None
    # Sub-origin exclusion (F1); None unless the spec declares `exclude`
    sub_origin_source: int | None = None  # all source rows, filtered or not
    sub_origin_excluded: int = 0
    sub_origin_excluded_keys: tuple[str, ...] = ()  # full, sorted
    sub_customer_ref_rows: tuple[str, ...] = ()  # keys refused: reference a Sub-tagged customer

    @property
    def refused(self) -> int:
        return len({e.source_key for e in self.exceptions})

    @property
    def census(self) -> dict[str, object] | None:
        """source = imported + excluded + refused, for `exclude` specs."""

        if self.sub_origin_source is None:
            return None
        accounted = self.written + self.sub_origin_excluded + self.refused
        return {
            "source": self.sub_origin_source,
            "imported": self.written,
            "excluded": self.sub_origin_excluded,
            "refused": self.refused,
            "balanced": accounted == self.sub_origin_source,
        }

    @property
    def complete(self) -> bool:
        census = self.census
        return (
            self.source_error is None
            and not self.exceptions
            and self.read == self.written
            and (census is None or bool(census["balanced"]))
        )


def _apply_deferred(
    destination_connection: sa.Connection,
    spec: TransformSpec,
    updates: list[tuple[str, Any, dict[str, Any]]],
    report: TransformReport,
) -> None:
    """Post-pass UPDATEs for deferred (self-FK) columns, by dest id.

    Runs after every insert (including the FK retry pass), so the
    referenced parent rows exist. Grouped by column set; batched
    with binary-split fallback. Updates matching no row (the row
    itself was exceptioned at insert) are no-ops; updates rejected
    by the destination become named exceptions.
    """

    if not updates:
        return
    by_columns: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for source_key, dest_id, values in updates:
        if dest_id is None:
            continue
        by_columns.setdefault(tuple(sorted(values)), []).append({"_src_key": source_key, "_dest_id": dest_id, **values})
    for columns, rows in by_columns.items():
        set_clause = ", ".join(f"{c} = :{c}" for c in columns)
        sql = sa.text(
            f"UPDATE {spec.destination_table} SET {set_clause} WHERE id = :_dest_id"  # noqa: S608
        )
        batch = [{k: v for k, v in r.items() if k != "_src_key"} for r in rows]
        keys = [r["_src_key"] for r in rows]
        _update_split(destination_connection, sql, batch, keys, spec, report)


def _update_split(
    destination_connection: sa.Connection,
    sql: sa.TextClause,
    rows: list[dict[str, Any]],
    keys: list[str],
    spec: TransformSpec,
    report: TransformReport,
) -> None:
    try:
        with destination_connection.begin_nested():
            destination_connection.execute(sql, rows)
        return
    except (sa.exc.IntegrityError, sa.exc.DataError) as exc:
        if len(rows) == 1:
            detail = str(exc.orig).splitlines()[0][:160] if exc.orig is not None else ""
            report.exceptions.append(
                RowException(
                    source_table=spec.source_table,
                    source_key=keys[0],
                    column="*",
                    reason=f"deferred update rejected ({type(exc.orig).__name__}): {detail}",
                )
            )
            return
    midpoint = len(rows) // 2
    _update_split(destination_connection, sql, rows[:midpoint], keys[:midpoint], spec, report)
    _update_split(destination_connection, sql, rows[midpoint:], keys[midpoint:], spec, report)


def execute_transform(
    source_connection: sa.Connection,
    destination_connection: sa.Connection,
    spec: TransformSpec,
    *,
    id_maps: dict[str, dict[Any, Any]] | None = None,
    run_constants: dict[str, Any] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> TransformReport:
    """Execute one table transform. Raises if the spec is not executable."""

    if spec.source_row_refusal is not None:
        refusal = spec.source_row_refusal
        # This path deliberately precedes insert executability, constants,
        # enrolment and FK checks. Census the complete source extent under
        # the declared Sub-origin policy; run constants cannot narrow it.
        for identifier in (*spec.source_table.split("."), refusal.source_key):
            if re.fullmatch(r"[a-z_][a-z0-9_]*", identifier) is None:
                raise ValueError("unsafe SQL identifier in source-row refusal")
        report = TransformReport(source_table=spec.source_table, destination_table=spec.destination_table)
        if any(gap.startswith("sub_origin_") for gap in spec.missing_required):
            report.source_error = "source_row_refusal_invalid_sub_origin_policy"
            return report
        exclude_sub = spec.sub_origin is SubOriginPolicy.EXCLUDE
        select_columns = f"{refusal.source_key}, {SUB_ORIGIN_COLUMN}" if exclude_sub else refusal.source_key
        excluded_keys: list[str] = []
        try:
            # Contain a failed PostgreSQL read in a savepoint so the caller's
            # snapshot transaction remains usable for the remaining diagnostics.
            # No outer transaction commit/rollback is performed here.
            with source_connection.begin_nested():
                if exclude_sub:
                    # Establish the complete frozen source count before streaming:
                    # a partial read must never masquerade as a complete census.
                    report.sub_origin_source = source_connection.execute(
                        sa.text(f"SELECT count(*) FROM {spec.source_table}")  # noqa: S608
                    ).scalar_one()
                rows = source_connection.execute(
                    sa.text(f"SELECT {select_columns} FROM {spec.source_table}"),  # noqa: S608
                    execution_options={"stream_results": True, "yield_per": 1000},
                )
                try:
                    for row in rows:
                        if exclude_sub and row[1] is not None:
                            excluded_keys.append(str(row[0]))
                            report.sub_origin_excluded += 1
                            continue
                        report.read += 1
                        report.exceptions.append(
                            RowException(spec.source_table, str(row[0]), refusal.source_key, refusal.reason)
                        )
                finally:
                    rows.close()
        except sa.exc.SQLAlchemyError:
            # Never confuse an unreadable source/key with a verified empty
            # table. Keep database error text (and possible payload) private.
            report.source_error = "source_row_refusal_census_unreadable"
        report.sub_origin_excluded_keys = tuple(sorted(excluded_keys))
        return report

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
    exclude_sub = spec.sub_origin is SubOriginPolicy.EXCLUDE
    where_sub = f" WHERE {spec.source_table}.{SUB_ORIGIN_COLUMN} IS NULL" if exclude_sub else ""
    ref = spec.sub_customer_ref
    select_columns = list(source_columns)
    if ref is not None and ref.column not in select_columns:
        select_columns.append(ref.column)
    select_sql = sa.text(
        f"SELECT {', '.join(select_columns)} FROM {spec.source_table}{where_sub}"  # noqa: S608
    )
    dest_columns = [
        c
        for c in dict.fromkeys(
            list(spec.column_map)
            + list(spec.constants)
            + list(spec.defaults)
            + list(spec.run_supplied)
            + list(spec.computed)
            + list(spec.lookups)
        )
        if c not in spec.deferred
    ]
    insert_sql = sa.text(
        # identifiers regex-validated in spec.py; row values are bound params
        f"INSERT INTO {spec.destination_table} ({', '.join(dest_columns)}) "  # noqa: S608
        f"VALUES ({', '.join(f':{c}' for c in dest_columns)})"
    )
    # Identify a stable source key for exception records: the column
    # feeding the destination `id`, else the first mapped column.
    guards = _fk_guards(destination_connection, spec, dest_columns) if not dry_run else ()
    key_source = spec.column_map.get("id", source_columns[0])
    if exclude_sub:
        # Excluded rows are never silent: counted, with sorted keys, so
        # the census (source = imported + excluded + refused) reconciles.
        report.sub_origin_source = (
            source_connection.execute(sa.text(f"SELECT count(*) FROM {spec.source_table}")).scalar() or 0  # noqa: S608
        )
        excluded_rows = source_connection.execute(
            sa.text(
                f"SELECT {key_source} FROM {spec.source_table} "  # noqa: S608
                f"WHERE {spec.source_table}.{SUB_ORIGIN_COLUMN} IS NOT NULL"
            )
        )
        excluded_keys = sorted(str(r[0]) for r in excluded_rows)
        report.sub_origin_excluded = len(excluded_keys)
        report.sub_origin_excluded_keys = tuple(excluded_keys)
    tagged_refs: set[str] = set()
    ref_refused: list[str] = []
    if ref is not None:
        # Fail closed: an unreadable customer table raises, never imports unchecked.
        tagged_refs = {
            norm_ref(r[0])
            for r in source_connection.execute(sa.text(tagged_customer_reference_sql(ref)))
            if r[0] is not None
        }
    batch: list[tuple[str, dict[str, Any]]] = []
    fk_retry: list[tuple[str, dict[str, Any]]] = []
    deferred_updates: list[tuple[str, Any, dict[str, Any]]] = []

    # Stream source rows server-side: buffered results made memory
    # (and wall time) grow superlinearly past ~70k journal rows at
    # Gate 4 - 70k rows in 30s but 160k never finished.
    source_rows = source_connection.execute(select_sql, execution_options={"stream_results": True, "yield_per": 1000})
    for row in source_rows.mappings():
        if limit is not None and report.read >= limit:
            break
        report.read += 1
        if ref is not None and row[ref.column] is not None and norm_ref(row[ref.column]) in tagged_refs:
            ref_refused.append(str(row[key_source]))
            report.exceptions.append(
                RowException(
                    source_table=spec.source_table,
                    source_key=str(row[key_source]),
                    column=ref.column,
                    reason=SUB_CUSTOMER_REFERENCE_REASON,
                )
            )
            continue
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
                # "column:" fallbacks resolve after the mapping loop.
            values[dest_col] = value
        for dest_col, strategy in spec.null_fallbacks.items():
            if strategy.startswith("column:") and values.get(dest_col) is None:
                values[dest_col] = values.get(strategy.removeprefix("column:"))
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
        guard_exceptions: list[RowException] = []
        for guard in guards:
            guard_key = _guard_key(values, guard.local_columns)
            if guard_key is None:
                continue
            if guard_key not in guard.parents:
                guard_exceptions.append(
                    RowException(
                        source_table=spec.source_table,
                        source_key=str(row[key_source]),
                        column=guard.local_columns[-1],
                        reason=(
                            f"foreign key parent absent in destination {guard.referred_table} ({guard.constraint})"
                        ),
                    )
                )
        if guard_exceptions:
            report.exceptions.extend(guard_exceptions)
            continue
        if not dry_run:
            params = values
            if spec.json_columns and any(values.get(c) is not None for c in spec.json_columns):
                params = {
                    k: json.dumps(v) if k in spec.json_columns and v is not None else v for k, v in values.items()
                }
            if any(isinstance(v, (dict, list)) for v in values.values()):
                # Raw text() inserts carry no column types, so psycopg
                # cannot adapt dict/list for JSONB columns - serialize
                # explicitly (Gate 4: mod_forms settings).
                params = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in values.items()}
            if destination_connection.dialect.name == "sqlite":
                # sqlite3 cannot bind Decimal; NUMERIC affinity stores
                # the float. Postgres (the rehearsal target) binds
                # Decimal natively and keeps exactness.
                params = {k: float(v) if isinstance(v, Decimal) else v for k, v in params.items()}
            deferred_values = {c: params.pop(c) for c in spec.deferred if params.get(c) is not None}
            for c in spec.deferred:
                params.pop(c, None)
            if deferred_values:
                deferred_updates.append((str(row[key_source]), params.get("id"), deferred_values))
            batch.append((str(row[key_source]), params))
            if len(batch) >= _BATCH_SIZE:
                _flush_batch(destination_connection, insert_sql, batch, spec, report, fk_retry)
                batch = []
    source_rows.close()
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
    _apply_deferred(destination_connection, spec, deferred_updates, report)
    report.sub_customer_ref_rows = tuple(sorted(ref_refused))
    if limit is not None and report.sub_origin_source is not None:
        report.sub_origin_source = None  # a limited run cannot reconcile a census
    if dry_run:
        # Expectation counts: rows that would have been written.
        report.written = report.read - len({e.source_key for e in report.exceptions})
    return report


__all__ = ["RowException", "TransformReport", "execute_transform"]
