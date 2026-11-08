"""Column-level transform specs: legacy shape -> module shape.

A spec is built by intersecting a destination table's columns with a
legacy source table's columns under explicit naming conventions. The
output never guesses silently:

- every destination column is mapped, renamed, or named in
  `missing_required` (non-nullable, no default, no source) — a spec
  with missing-required columns is not executable;
- every source column is consumed or named in `residue` — residue is
  ERP-unique data that must land in `e2` storage or be retired by an
  explicit ruling at the wave rehearsal, never dropped by omission.

Systematic legacy conventions (verified over the 2026-11-08 model
census): the legacy primary key is `<singular>_id` where the module
uses `id`, and the tenant column is `organization_id` where the
module uses `tenant_id`. Per-table renames layer on top.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _check(identifier: str) -> str:
    if not _IDENTIFIER.match(identifier):
        raise ValueError(f"unsafe SQL identifier in transform spec: {identifier!r}")
    return identifier


_NO_DEFAULT = object()


@dataclass(frozen=True)
class DestinationColumn:
    name: str
    nullable: bool
    has_default: bool  # SERVER default only: raw-SQL inserts never see
    # Python-side column defaults, so those do not satisfy a column.
    scalar_default: object = _NO_DEFAULT
    # A module-declared Python SCALAR default (bool/int/str/Decimal):
    # part of the destination shape, surfaced on the spec as a
    # default the executor writes explicitly. Callable defaults
    # (uuid4, now, dict) never qualify - identity and timestamps
    # must come from the source row or stay missing.


@dataclass(frozen=True)
class TransformSpec:
    source_table: str  # schema-qualified, e.g. public.employee
    destination_table: str  # schema-qualified, e.g. mod_people.employees
    column_map: Mapping[str, str]  # destination column -> source column
    missing_required: tuple[str, ...]
    residue: tuple[str, ...]  # source columns no destination consumes
    fk_rewrites: Mapping[str, str] = field(default_factory=dict)
    # destination FK column -> name of the id-map that rewrites its values
    constants: Mapping[str, object] = field(default_factory=dict)
    # destination column -> explicit enrolment constant (e.g. source_ref).
    # Constants are declared in the wave pairing, never invented here;
    # a required column with neither source nor constant stays missing.
    defaults: Mapping[str, object] = field(default_factory=dict)
    # destination column -> module-declared scalar default carried
    # from DestinationColumn (see there). Written explicitly by the
    # executor; pairing constants override them.
    run_supplied: tuple[str, ...] = ()
    # Required columns the import RUN supplies per tenant extent
    # (tenant_id, minor_units, actor provenance). Executable only when
    # the executor is given values for every one of them.
    computed: Mapping[str, str] = field(default_factory=dict)
    # destination column -> computed strategy: values derived
    # deterministically from the mapped row. Closed set: "sha256_row"
    # (fingerprints/digests), "source_key" (stable legacy key, e.g.
    # idempotency/submission keys), "slug:<dest column>" (human key
    # slugified from an already-mapped destination value, e.g.
    # template codes legacy never stored), "mul:<dest a>:<dest b>"
    # (exact Decimal product of two mapped values, e.g. a gross
    # amount legacy stored only as quantity x unit price),
    # "uuid5_row" (deterministic CHAR(32) id for natural-key
    # legacy tables with no UUID of their own, e.g. bank_directory).
    deferred: tuple[str, ...] = ()
    # destination columns excluded from the INSERT and applied by
    # the executor in a post-pass UPDATE keyed by destination id.
    # For self-referential FKs (journal reversal links, department
    # parents): heap order interleaves parents and children, so a
    # direct insert FK-fails a few percent of rows and poisons
    # nearly every insert batch into split fallbacks (Gate 4:
    # ~39k FK errors, journal import 10x slower). Deferred columns
    # must be nullable and mapped; updates that still fail (parent
    # row itself exceptioned) become named exceptions.
    null_fallbacks: Mapping[str, str] = field(default_factory=dict)
    # destination column -> fallback applied by the executor ONLY
    # when the mapped source value is NULL. Closed set: "row_id"
    # (the row's own source key - the legacy row is its own source
    # document of record), "source_key" (legacy:<table>:<key>),
    # "literal:<text>", "run:<constant>" (a declared run constant,
    # e.g. the enrolment actor for created_by_id), "column:<dest>"
    # (another mapped destination value in the same row, e.g.
    # original amounts falling back to functional amounts when the
    # legacy posting never recorded a separate original leg).
    # Fallbacks never
    # invent external-world data; columns whose NULL means "the
    # fact was never recorded" (e.g. a supplier's document number)
    # must exception instead.
    lookups: Mapping[str, tuple[str, str, str]] = field(default_factory=dict)
    # destination column -> (map name, key destination column, field):
    # the value is read at execution from a caller-supplied lookup map
    # keyed by an already-mapped destination column's value (module
    # tables denormalize e.g. component code/name/kind onto rules and
    # calculation lines; the enrolment step builds the map from the
    # imported parent rows). A key with no map entry is an exception,
    # never a default.

    @property
    def executable(self) -> bool:
        return not self.missing_required


def _is_run_supplied(dest: str) -> bool:
    """Columns an import run supplies per tenant extent, by convention."""

    return (
        dest in ("tenant_id", "minor_units", "source_owner", "source_version")
        or dest.endswith("_by_id")
        or dest.endswith("_by_ref")
        or dest == "posted_by"
    )


def _systematic_candidates(dest: str, source_columns: set[str], source_table: str) -> str | None:
    """Legacy naming conventions, in priority order."""

    if dest == "tenant_id" and "organization_id" in source_columns:
        return "organization_id"
    if dest == "id":
        raw = source_table.rsplit(".", 1)[-1]
        # Legacy PK naming varies: full table name (department_id),
        # shortened suffix forms (posted_ledger_line -> ledger_line_id,
        # salary_slip -> slip_id), and prefix forms (fuel_log_entry ->
        # fuel_log_id, lease_contract -> lease_id). Try raw and
        # s-stripped token sets - naive singularization mangles
        # legitimately s-ending names (case_witness -> witness_id,
        # not witnes_id; found live at Gate 4 rehearsal).
        candidates: list[str] = []
        for base in (raw, raw.removesuffix("s")):
            tokens = base.split("_")
            candidates.append(f"{base}_id")
            candidates.append(f"{base}id")
            if len(tokens) >= 2:
                candidates.append(f"{tokens[-2]}_{tokens[-1]}_id")
            candidates.append(f"{tokens[-1]}_id")
        tokens = raw.split("_")
        if len(tokens) >= 2:
            candidates.append(f"{tokens[0]}_{tokens[1]}_id")
        candidates.append(f"{tokens[0]}_id")
        for candidate in dict.fromkeys(candidates):
            if candidate in source_columns:
                return candidate
    if dest == "party_id" and "person_id" in source_columns:
        return "person_id"
    # Legacy tables prefix their own code/name/number/type columns with
    # the singular table name (account_code, item_code, po_number...);
    # modules use the bare names. Opaque module refs (`supplier_ref`)
    # take the legacy FK (`supplier_id`) stringified at enrolment.
    singular = source_table.rsplit(".", 1)[-1].removesuffix("s")
    prefixed = {
        "code": (f"{singular}_code",),
        "name": (f"{singular}_name", f"{singular}_title"),
        "number": (f"{singular}_number",),
        "sku": (f"{singular}_code",),
        "kind": (f"{singular}_type", "type", f"{singular}_kind"),
        "type": (f"{singular}_type",),
        "title": ("subject", f"{singular}_name", f"{singular}_title"),
        "priority": (f"{singular}_priority",),
    }
    for candidate in prefixed.get(dest, ()):
        if candidate in source_columns:
            return candidate
    # Table-qualified code/name where the table name is compound
    # (account_category.category_code, fiscal_year.year_code...):
    # accept a unique *_code / *_name once obvious foreign references
    # are excluded. Ambiguity stays unmapped, never guessed.
    if dest in ("code", "name"):
        suffix = f"_{dest}"
        foreign = {"currency_code", "country_code", "bank_name", "employee_name"}
        candidates = sorted(c for c in source_columns if c.endswith(suffix) and c not in foreign)
        if len(candidates) == 1:
            return candidates[0]
    if dest.endswith("_ref"):
        candidate = f"{dest.removesuffix('_ref')}_id"
        if candidate in source_columns:
            return candidate
    return None


def build_spec(
    *,
    source_table: str,
    destination_table: str,
    source_columns: Sequence[str],
    destination_columns: Sequence[DestinationColumn],
    renames: Mapping[str, str] | None = None,
    fk_rewrites: Mapping[str, str] | None = None,
    constants: Mapping[str, object] | None = None,
    lookups: Mapping[str, tuple[str, str, str]] | None = None,
    computed: Mapping[str, str] | None = None,
    null_fallbacks: Mapping[str, str] | None = None,
    deferred: tuple[str, ...] = (),
) -> TransformSpec:
    """Intersect shapes into a spec; name every gap, never fill one."""

    for part in source_table.split(".") + destination_table.split("."):
        _check(part)
    renames = dict(renames or {})
    constants = dict(constants or {})
    lookups = dict(lookups or {})
    null_fallbacks = dict(null_fallbacks or {})
    for dest_col, strategy in null_fallbacks.items():
        _check(dest_col)
        if strategy in ("row_id", "source_key"):
            continue
        if strategy.startswith("literal:") and strategy.removeprefix("literal:"):
            continue
        if strategy.startswith("run:"):
            _check(strategy.removeprefix("run:"))
            continue
        if strategy.startswith("column:"):
            _check(strategy.removeprefix("column:"))
            continue
        raise ValueError(f"unknown null fallback {strategy!r} for {dest_col!r}")
    explicit_computed = dict(computed or {})
    for dest_col, strategy in explicit_computed.items():
        _check(dest_col)
        if strategy in ("sha256_row", "source_key", "uuid5_row"):
            continue
        if strategy.startswith("slug:"):
            _check(strategy.removeprefix("slug:"))
            continue
        if strategy.startswith("mul:"):
            factors = strategy.removeprefix("mul:").split(":")
            if len(factors) != 2:
                raise ValueError(f"mul strategy needs two factors: {strategy!r}")
            for factor in factors:
                _check(factor)
            continue
        raise ValueError(f"unknown computed strategy {strategy!r} for {dest_col!r}")
    for dest_col, (map_name, key_col, lookup_field) in lookups.items():
        _check(dest_col)
        _check(map_name)
        _check(key_col)
        _check(lookup_field)
    source_set = {_check(c) for c in source_columns}
    column_map: dict[str, str] = {}
    missing: list[str] = []
    run_supplied: list[str] = []
    computed: dict[str, str] = {}
    defaults: dict[str, object] = {}
    for dest in destination_columns:
        _check(dest.name)
        if dest.name in constants:
            continue
        if dest.name in lookups:
            continue
        if dest.name in explicit_computed:
            continue
        if dest.name in renames:
            source = renames[dest.name]
            if source not in source_set:
                missing.append(dest.name)
                continue
        elif dest.name in source_set:
            source = dest.name
        else:
            found = _systematic_candidates(dest.name, source_set, source_table)
            if found is None:
                # No source at all: computed and run-supplied conventions
                # apply before declaring the column missing.
                if "fingerprint" in dest.name or dest.name.endswith("_sha256") or dest.name.endswith("_digest"):
                    computed[dest.name] = "sha256_row"
                elif dest.name in ("evidence_ref", "source_ref", "source_reference"):
                    computed[dest.name] = "source_key"
                elif _is_run_supplied(dest.name):
                    if not dest.nullable and not dest.has_default:
                        run_supplied.append(dest.name)
                elif dest.scalar_default is not _NO_DEFAULT:
                    defaults[dest.name] = dest.scalar_default
                elif not dest.nullable and not dest.has_default:
                    missing.append(dest.name)
                continue
            source = found
        column_map[dest.name] = source
    computed = {**computed, **explicit_computed}
    for dest_col, strategy in computed.items():
        if strategy.startswith("slug:") and strategy.removeprefix("slug:") not in column_map:
            raise ValueError(f"slug for {dest_col!r} reads unmapped column {strategy!r}")
        if strategy.startswith("mul:"):
            unmapped = [f for f in strategy.removeprefix("mul:").split(":") if f not in column_map]
            if unmapped:
                raise ValueError(f"mul for {dest_col!r} reads unmapped columns {unmapped}")
    for dest_col, (_map, key_col, _field) in lookups.items():
        if key_col not in column_map:
            raise ValueError(f"lookup for {dest_col!r} keys on unmapped column {key_col!r}")
    for dest_col in deferred:
        _check(dest_col)
        if dest_col not in column_map:
            raise ValueError(f"deferred column {dest_col!r} is not mapped from the source")
    consumed = set(column_map.values())
    residue = tuple(sorted(source_set - consumed))
    return TransformSpec(
        source_table=source_table,
        destination_table=destination_table,
        column_map=column_map,
        missing_required=tuple(missing),
        residue=residue,
        fk_rewrites=dict(fk_rewrites or {}),
        constants=constants,
        defaults=defaults,
        run_supplied=tuple(run_supplied),
        computed=computed,
        lookups=lookups,
        null_fallbacks=null_fallbacks,
        deferred=tuple(deferred),
    )


def _scalar_default(col: object) -> object:
    """A column's Python scalar default, or _NO_DEFAULT when callable/other."""

    default = getattr(col, "default", None)
    if default is None or getattr(default, "is_callable", True) or getattr(default, "is_sequence", True):
        return _NO_DEFAULT
    arg = getattr(default, "arg", _NO_DEFAULT)
    if isinstance(arg, bool | int | float | str) or type(arg).__name__ == "Decimal":
        return arg
    return _NO_DEFAULT


def destination_columns_from_table(table: object) -> tuple[DestinationColumn, ...]:
    """Adapt a SQLAlchemy Table (module metadata) into DestinationColumns.

    `has_default` counts SERVER defaults only: the executor issues
    raw SQL inserts, where Python-side defaults (uuid4 ids, bool
    flags) never fire - omitting such a column would fail the
    insert outright on Postgres. Python scalar defaults are still
    honoured, as explicit spec defaults (see DestinationColumn).
    """

    columns = getattr(table, "columns", None)
    if columns is None:
        raise TypeError("expected a SQLAlchemy Table-like object with .columns")
    return tuple(
        DestinationColumn(
            name=col.name,
            nullable=bool(col.nullable),
            has_default=col.server_default is not None,
            scalar_default=_scalar_default(col),
        )
        for col in columns
    )


__all__ = [
    "DestinationColumn",
    "TransformSpec",
    "build_spec",
    "destination_columns_from_table",
]
