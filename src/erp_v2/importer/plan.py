"""Dry-run import planner: pure, database-free, refuses open work.

The planner orders a wave's table mappings into a dependency-respecting
sequence and refuses to produce a plan while any mapping carries an
unruled residue (`needs_ruling`) — the Academy rule that an import is
never built from a partial snapshot with unresolved exceptions. The
destination heads come from the assembly's own EXPECTED_HEADS, so a
plan cannot be silently built against a different composed lineage
than the one this repository migrates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from erp_v2.migrate import EXPECTED_HEADS


class PlanRefusal(RuntimeError):
    """The requested mapping set is not importable as declared."""


@dataclass(frozen=True)
class TableMapping:
    """One legacy table's transform-to-fit ruling."""

    source: str  # legacy table, schema-qualified (e.g. mod_files.stored_files)
    destination: str  # module/kernel/e2 table, or an explicit action
    kind: str  # "transform" | "carry" (already module storage) | "retire"
    depends_on: tuple[str, ...] = ()  # source tables that must move first
    needs_ruling: str | None = None  # unresolved question; blocks the plan


@dataclass(frozen=True)
class ImportPlan:
    destination_heads: tuple[str, ...]
    steps: tuple[TableMapping, ...] = field(default_factory=tuple)


def build_plan(mappings: tuple[TableMapping, ...]) -> ImportPlan:
    """Order mappings, or refuse naming every unruled residue."""

    unruled = [m for m in mappings if m.needs_ruling]
    if unruled:
        detail = "; ".join(f"{m.source}: {m.needs_ruling}" for m in unruled)
        raise PlanRefusal(f"unruled table mappings block the import: {detail}")

    sources = [m.source for m in mappings]
    if len(set(sources)) != len(sources):
        raise PlanRefusal("duplicate source table in mapping set")

    by_source = {m.source: m for m in mappings}
    for mapping in mappings:
        unknown = set(mapping.depends_on) - set(by_source)
        if unknown:
            raise PlanRefusal(f"{mapping.source} depends on unmapped source(s): " + ", ".join(sorted(unknown)))
    ordered: list[TableMapping] = []
    placed: set[str] = set()
    remaining = list(mappings)
    while remaining:
        ready = [m for m in remaining if set(m.depends_on) <= placed]
        if not ready:
            cycle = ", ".join(sorted(m.source for m in remaining))
            raise PlanRefusal(f"unresolvable mapping dependencies: {cycle}")
        for mapping in ready:
            ordered.append(mapping)
            placed.add(mapping.source)
            remaining.remove(mapping)

    return ImportPlan(destination_heads=tuple(sorted(EXPECTED_HEADS)), steps=tuple(ordered))


__all__ = ["ImportPlan", "PlanRefusal", "TableMapping", "build_plan"]
