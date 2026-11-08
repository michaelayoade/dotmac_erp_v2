"""Enrolment maps: builders for every id-map the specs declare.

Transform specs reference maps by name in two places (see spec.py):

- `fk_rewrites`: scalar value -> value (e.g. a legacy structure id
  becomes the generated revision id);
- `lookups`: key -> {field: value} (module tables that denormalize
  parent facts, e.g. component code/name/kind on payroll rules).

This module builds each declared map from its authoritative side:

- **static** value tables ruled in the pairing notes;
- **source** maps read from the legacy snapshot (identity links and
  parent rows whose UUIDs are preserved by the import);
- **destination** maps read from already-imported or enrolment-minted
  module rows. Builders only READ: minting the generated rows
  (pay-structure revisions, tax authorities, sourcing lines) is a
  separate enrolment action that must run before the specs that
  consume these maps. Until then the map is empty and dependent
  rows exception - loudly, in the receipt.

Rules: a key with two different values is a MapConflict, never a
silent pick. UUID keys/values are normalized to the 32-char hex form
module CHAR(32) ids use, so Postgres UUID objects and hex strings
meet in the middle (executor.py tries the same candidate forms).
Maps are keyed by globally-unique UUIDs, so they are tenant-extent
agnostic; the tenant extent still bounds which source rows import.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

import sqlalchemy as sa

from erp_v2.importer.spec import TransformSpec


class MapConflict(ValueError):
    """One enrolment-map key produced two different values."""


def _norm(value: Any) -> Any:
    """UUID objects -> 32-char hex (the module id form); else unchanged."""

    return value.hex if isinstance(value, uuid.UUID) else value


def _put(mapping: dict[Any, Any], map_name: str, key: Any, value: Any) -> None:
    key = _norm(key)
    if key is None:
        return
    existing = mapping.get(key)
    if existing is not None and existing != value:
        raise MapConflict(f"enrolment map {map_name!r}: key {key!r} maps to both {existing!r} and {value!r}")
    mapping[key] = value


def _scalar(rows: Any, map_name: str) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key, value in rows:
        _put(mapping, map_name, key, _norm(value))
    return mapping


def _lookup(rows: Any, map_name: str, fields: Sequence[str]) -> dict[Any, dict[str, Any]]:
    mapping: dict[Any, dict[str, Any]] = {}
    for row in rows:
        entry = {field: _norm(row[field]) for field in fields}
        _put(mapping, map_name, row["key"], entry)
    return mapping


# --- static -----------------------------------------------------------------


def _bank_direction_by_type(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Module check constraint: direction IN ('credit','debit'). Only the
    # literal legacy spellings are ruled; anything else exceptions.
    return {"CREDIT": "credit", "DEBIT": "debit", "credit": "credit", "debit": "debit"}


# --- source (legacy snapshot) -----------------------------------------------


def _employee_party(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Legacy employee.person_id IS the kernel Party UUID (preserved).
    rows = source.execute(sa.text("SELECT employee_id, person_id FROM public.employee"))
    return _scalar(rows, "employee_party")


def _user_party(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Legacy user_credentials.id is the user UUID created_by columns
    # reference; person_id is the kernel Party UUID (preserved).
    rows = source.execute(sa.text("SELECT id, person_id FROM public.user_credentials"))
    return _scalar(rows, "user_party")


def _supplier(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text(
            "SELECT supplier_id AS key, COALESCE(NULLIF(legal_name, ''), trading_name) AS name FROM public.supplier"
        )
    ).mappings()
    return _lookup(rows, "supplier", ("name",))


def _payment_intent_by_payment(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text(
            "SELECT customer_payment_id AS key, intent_id AS intent_id "
            "FROM public.payment_intent WHERE customer_payment_id IS NOT NULL"
        )
    ).mappings()
    return _lookup(rows, "payment_intent_by_payment", ("intent_id",))


def _project_template_task(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text("SELECT template_task_id AS key, template_id AS template_id FROM public.project_template_task")
    ).mappings()
    return _lookup(rows, "project_template_task", ("template_id",))


def _bid_submission(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    # UUIDs are preserved, so the legacy rfq_id IS the module event id.
    rows = source.execute(
        sa.text("SELECT response_id AS key, rfq_id AS event_id FROM public.quotation_response")
    ).mappings()
    return _lookup(rows, "bid_submission", ("event_id",))


# --- destination (imported / enrolment-minted module rows) -----------------


def _structure_revision(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Enrolment mints one published v1 revision per structure; the
    # import binds to v1 by ruling (later versions are post-flip facts).
    rows = dest.execute(sa.text("SELECT structure_id, id FROM mod_payroll.pay_structure_revisions WHERE version = 1"))
    return _scalar(rows, "structure_revision")


def _tax_authority_by_level(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Enrolment mints one authority per legacy jurisdiction level with
    # authority_level_code carrying the legacy level value.
    rows = dest.execute(sa.text("SELECT authority_level_code, id FROM mod_tax.tax_authorities"))
    return _scalar(rows, "tax_authority_by_level")


def _sourcing_line_by_requisition_line(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    rows = dest.execute(
        sa.text(
            "SELECT source_requisition_line_id, id FROM mod_procurement.sourcing_event_lines "
            "WHERE source_requisition_line_id IS NOT NULL"
        )
    )
    return _scalar(rows, "sourcing_line_by_requisition_line")


def _bank_institution_by_code(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    rows = dest.execute(sa.text("SELECT code, id FROM mod_banking.bank_institutions"))
    return _scalar(rows, "bank_institution_by_code")


def _fiscal_period(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = dest.execute(sa.text("SELECT id AS key, end_date AS end_date FROM mod_accounting.fiscal_periods")).mappings()
    return _lookup(rows, "fiscal_period", ("end_date",))


def _inventory_item(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = dest.execute(sa.text("SELECT id AS key, base_uom AS base_uom FROM mod_inventory.items")).mappings()
    return _lookup(rows, "inventory_item", ("base_uom",))


def _pay_component(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    # code/name/kind from the IMPORTED components (module vocabulary
    # wins); prorates from the legacy depends_on_payment_days flag,
    # which the module stores on rules, not on components.
    prorates = {
        _norm(key): bool(flag)
        for key, flag in source.execute(
            sa.text("SELECT component_id, depends_on_payment_days FROM public.salary_component")
        )
    }
    mapping: dict[Any, dict[str, Any]] = {}
    rows = dest.execute(sa.text("SELECT id, code, name, kind FROM mod_payroll.pay_components")).mappings()
    for row in rows:
        key = _norm(row["id"])
        if key not in prorates:
            raise MapConflict(
                f"enrolment map 'pay_component': imported component {key!r} has no legacy prorates source"
            )
        _put(
            mapping,
            "pay_component",
            key,
            {"code": row["code"], "name": row["name"], "kind": row["kind"], "prorates": prorates[key]},
        )
    return mapping


MAP_BUILDERS: Final[Mapping[str, Any]] = {
    "bank_direction_by_type": _bank_direction_by_type,
    "bank_institution_by_code": _bank_institution_by_code,
    "bid_submission": _bid_submission,
    "employee_party": _employee_party,
    "fiscal_period": _fiscal_period,
    "inventory_item": _inventory_item,
    "pay_component": _pay_component,
    "payment_intent_by_payment": _payment_intent_by_payment,
    "project_template_task": _project_template_task,
    "sourcing_line_by_requisition_line": _sourcing_line_by_requisition_line,
    "structure_revision": _structure_revision,
    "supplier": _supplier,
    "tax_authority_by_level": _tax_authority_by_level,
    "user_party": _user_party,
}


def required_maps(specs: Sequence[TransformSpec]) -> frozenset[str]:
    """Every map name the given specs declare (rewrites + lookups)."""

    names: set[str] = set()
    for spec in specs:
        names.update(spec.fk_rewrites.values())
        names.update(map_name for map_name, _key, _field in spec.lookups.values())
    return frozenset(names)


def unbuilt_maps(specs: Sequence[TransformSpec]) -> frozenset[str]:
    """Declared maps with no builder - must stay empty (coverage test)."""

    return required_maps(specs) - MAP_BUILDERS.keys()


class EnrolmentMaps:
    """Lazy, cached access to enrolment maps over caller-owned connections.

    A destination-built map is read at first need, so spec order in
    the run (parents before consumers) is what makes it complete.
    The cache is per-instance: one instance per import run.
    """

    def __init__(self, source_connection: sa.Connection, destination_connection: sa.Connection) -> None:
        self._source = source_connection
        self._dest = destination_connection
        self._cache: dict[str, dict[Any, Any]] = {}

    def get(self, name: str) -> dict[Any, Any]:
        if name not in self._cache:
            builder = MAP_BUILDERS.get(name)
            if builder is None:
                raise KeyError(f"no enrolment builder for map {name!r}")
            self._cache[name] = builder(self._source, self._dest)
        return self._cache[name]

    def for_spec(self, spec: TransformSpec) -> dict[str, dict[Any, Any]]:
        return {name: self.get(name) for name in sorted(required_maps((spec,)))}

    def summary(self) -> dict[str, int]:
        """Built map sizes, for the import receipt."""

        return {name: len(mapping) for name, mapping in sorted(self._cache.items())}


__all__ = [
    "MAP_BUILDERS",
    "EnrolmentMaps",
    "MapConflict",
    "required_maps",
    "unbuilt_maps",
]
