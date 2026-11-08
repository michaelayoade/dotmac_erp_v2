"""Import pipeline: ordered transform execution + private receipt.

One run executes an ordered spec sequence (identity/kernel first,
then module waves — ordering is the caller's plan, see plan.py) and
aggregates every table report into a single receipt. The receipt is
the rehearsal/flip evidence unit: candidate heads, per-table
read/written counts, and the full exception queue. A run whose
receipt is not `complete` must never be flipped to.

The caller owns connections and the destination transaction. In
dry-run the destination connection is still required (specs are
validated against it by the caller's harness) but nothing is
written; the receipt then carries expectation counts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from erp_v2.importer.enrolment import EnrolmentMaps
from erp_v2.importer.executor import RowException, TransformReport, execute_transform
from erp_v2.importer.minting import MintResult
from erp_v2.importer.spec import TransformSpec
from erp_v2.migrate import EXPECTED_HEADS


def order_specs(
    specs: Sequence[TransformSpec],
    destination_tables: Mapping[str, Any],
) -> tuple[TransformSpec, ...]:
    """Topologically order specs so FK parents land before children.

    Gate 4 live finding: wave declaration order put employees
    before their party parents - every row FK-rejected. The
    destination metadata FK graph is the authority for execution
    order; declaration order only breaks ties (stable). Specs
    sharing a destination stay together in declaration order.
    Cycles (mutual FKs) keep declaration order for the remainder;
    the executor's FK retry pass covers intra-run ordering.
    """

    def _key(table: Any) -> str:
        return f"{table.schema}.{table.name}" if table.schema else table.name

    groups: dict[str, list[TransformSpec]] = {}
    for spec in specs:
        groups.setdefault(spec.destination_table, []).append(spec)
    parents: dict[str, set[str]] = {}
    for dest in groups:
        table = destination_tables.get(dest)
        deps: set[str] = set()
        if table is not None:
            for fk in table.foreign_keys:
                parent_key = _key(fk.column.table)
                if parent_key in groups and parent_key != dest:
                    deps.add(parent_key)
        parents[dest] = deps
    ordered_dests: list[str] = []
    emitted: set[str] = set()
    remaining = list(groups)
    while remaining:
        ready = [d for d in remaining if parents[d] <= emitted]
        if not ready:  # cycle: declaration order for the rest
            ordered_dests.extend(remaining)
            break
        for dest in ready:
            ordered_dests.append(dest)
            emitted.add(dest)
            remaining.remove(dest)
    return tuple(spec for dest in ordered_dests for spec in groups[dest])


@dataclass(frozen=True)
class ImportReceipt:
    destination_heads: tuple[str, ...]
    dry_run: bool
    reports: tuple[TransformReport, ...] = field(default_factory=tuple)
    enrolment_maps: Mapping[str, int] = field(default_factory=dict)
    # map name -> entry count actually built during the run (evidence
    # that rewrites/lookups ran against real enrolment data)
    mints: tuple[MintResult, ...] = field(default_factory=tuple)
    # GENERATED-row minting outcomes (created/existing/blockers/refused)

    @property
    def read(self) -> int:
        return sum(r.read for r in self.reports)

    @property
    def written(self) -> int:
        return sum(r.written for r in self.reports)

    @property
    def exceptions(self) -> tuple[RowException, ...]:
        return tuple(e for r in self.reports for e in r.exceptions)

    @property
    def complete(self) -> bool:
        return all(r.complete for r in self.reports)


def run_import(
    source_connection: sa.Connection,
    destination_connection: sa.Connection,
    specs: Sequence[TransformSpec],
    *,
    id_maps: Mapping[str, dict[Any, Any]] | None = None,
    enrolment: EnrolmentMaps | None = None,
    run_constants: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    commit_per_spec: bool = False,
) -> ImportReceipt:
    """Execute specs in the given order; never skip a named failure.

    A non-executable spec raises before its table runs (earlier
    tables' writes remain the caller's transaction to roll back) —
    building a partial import behind a green receipt is the failure
    mode this pipeline exists to prevent.

    Per spec, enrolment builds (lazily, cached) exactly the maps
    that spec declares; explicitly passed `id_maps` override the
    enrolment-built map of the same name.

    `commit_per_spec` (apply runs): commit the destination after
    each spec so landed tables stay landed and lock growth stays
    bounded - one mega-transaction across ~780k rows exhausted
    the lock table and made all progress disposable (Gate 4).
    """

    explicit = dict(id_maps or {})
    reports: list[TransformReport] = []
    for spec in specs:
        spec_maps = enrolment.for_spec(spec) if enrolment is not None else {}
        reports.append(
            execute_transform(
                source_connection,
                destination_connection,
                spec,
                id_maps={**spec_maps, **explicit},
                run_constants=dict(run_constants or {}),
                dry_run=dry_run,
            )
        )
        if commit_per_spec and not dry_run:
            destination_connection.commit()
    return ImportReceipt(
        destination_heads=tuple(sorted(EXPECTED_HEADS)),
        dry_run=dry_run,
        reports=tuple(reports),
        enrolment_maps=enrolment.summary() if enrolment is not None else {},
        mints=enrolment.mint_results() if enrolment is not None else (),
    )


__all__ = ["ImportReceipt", "run_import"]
