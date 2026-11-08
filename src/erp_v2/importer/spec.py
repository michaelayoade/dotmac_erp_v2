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


@dataclass(frozen=True)
class DestinationColumn:
    name: str
    nullable: bool
    has_default: bool


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
    run_supplied: tuple[str, ...] = ()
    # Required columns the import RUN supplies per tenant extent
    # (tenant_id, minor_units, actor provenance). Executable only when
    # the executor is given values for every one of them.
    computed: Mapping[str, str] = field(default_factory=dict)
    # destination column -> computed strategy ("sha256_row"): values
    # derived deterministically from the mapped source row, e.g.
    # source/request fingerprints modules use for idempotent replay.

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
        singular = source_table.rsplit(".", 1)[-1].removesuffix("s")
        for candidate in (f"{singular}_id", f"{singular}id"):
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
) -> TransformSpec:
    """Intersect shapes into a spec; name every gap, never fill one."""

    for part in source_table.split(".") + destination_table.split("."):
        _check(part)
    renames = dict(renames or {})
    constants = dict(constants or {})
    source_set = {_check(c) for c in source_columns}
    column_map: dict[str, str] = {}
    missing: list[str] = []
    run_supplied: list[str] = []
    computed: dict[str, str] = {}
    for dest in destination_columns:
        _check(dest.name)
        if dest.name in constants:
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
                if "fingerprint" in dest.name:
                    computed[dest.name] = "sha256_row"
                elif dest.name in ("evidence_ref", "source_ref", "source_reference"):
                    computed[dest.name] = "source_key"
                elif _is_run_supplied(dest.name):
                    if not dest.nullable and not dest.has_default:
                        run_supplied.append(dest.name)
                elif not dest.nullable and not dest.has_default:
                    missing.append(dest.name)
                continue
            source = found
        column_map[dest.name] = source
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
        run_supplied=tuple(run_supplied),
        computed=computed,
    )


def destination_columns_from_table(table: object) -> tuple[DestinationColumn, ...]:
    """Adapt a SQLAlchemy Table (module metadata) into DestinationColumns."""

    columns = getattr(table, "columns", None)
    if columns is None:
        raise TypeError("expected a SQLAlchemy Table-like object with .columns")
    return tuple(
        DestinationColumn(
            name=col.name,
            nullable=bool(col.nullable),
            has_default=col.default is not None or col.server_default is not None,
        )
        for col in columns
    )


__all__ = [
    "DestinationColumn",
    "TransformSpec",
    "build_spec",
    "destination_columns_from_table",
]
