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
) -> ImportReceipt:
    """Execute specs in the given order; never skip a named failure.

    A non-executable spec raises before its table runs (earlier
    tables' writes remain the caller's transaction to roll back) —
    building a partial import behind a green receipt is the failure
    mode this pipeline exists to prevent.

    Per spec, enrolment builds (lazily, cached) exactly the maps
    that spec declares; explicitly passed `id_maps` override the
    enrolment-built map of the same name.
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
    return ImportReceipt(
        destination_heads=tuple(sorted(EXPECTED_HEADS)),
        dry_run=dry_run,
        reports=tuple(reports),
        enrolment_maps=enrolment.summary() if enrolment is not None else {},
        mints=enrolment.mint_results() if enrolment is not None else (),
    )


__all__ = ["ImportReceipt", "run_import"]
