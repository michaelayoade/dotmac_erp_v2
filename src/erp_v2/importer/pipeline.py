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

from erp_v2.importer.executor import RowException, TransformReport, execute_transform
from erp_v2.importer.spec import TransformSpec
from erp_v2.migrate import EXPECTED_HEADS


@dataclass(frozen=True)
class ImportReceipt:
    destination_heads: tuple[str, ...]
    dry_run: bool
    reports: tuple[TransformReport, ...] = field(default_factory=tuple)

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
    dry_run: bool = False,
) -> ImportReceipt:
    """Execute specs in the given order; never skip a named failure.

    A non-executable spec raises before its table runs (earlier
    tables' writes remain the caller's transaction to roll back) —
    building a partial import behind a green receipt is the failure
    mode this pipeline exists to prevent.
    """

    reports = tuple(
        execute_transform(
            source_connection,
            destination_connection,
            spec,
            id_maps=dict(id_maps or {}),
            dry_run=dry_run,
        )
        for spec in specs
    )
    return ImportReceipt(
        destination_heads=tuple(sorted(EXPECTED_HEADS)),
        dry_run=dry_run,
        reports=reports,
    )


__all__ = ["ImportReceipt", "run_import"]
