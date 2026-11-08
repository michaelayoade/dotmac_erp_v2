"""Lifecycle staging: module insertion guards vs historical rows.

Three module families refuse historical inserts by trigger: fiscal
periods must begin FUTURE, journals must be assembled DRAFT, and
payables documents must begin DRAFT (lines are immutable once the
parent leaves DRAFT). The specs therefore force the initial status
via pairing constants, and this post-spec step walks each imported
row through the module's legal transitions to its legacy final
state, after every child row has landed:

- fiscal periods: FUTURE -> OPEN -> SOFT_CLOSED (legacy states)
- journals: DRAFT -> POSTED (the module trigger verifies balanced
  posted-ledger evidence per journal - a real parity check),
  DRAFT -> VOID, then POSTED -> REVERSED for reversed originals.
  Legacy APPROVED/SUBMITTED have no module journal status (approval
  is evidence on a posting, not a state): those rows stay DRAFT
  and are named exceptions for accounting review.
- supplier invoices: DRAFT -> SUBMITTED -> APPROVED; legacy
  POSTED/PARTIALLY_PAID/PAID are downstream of approval in the
  module (settlement derives from settlement observations), so
  they land APPROVED. REJECTED has no module status: named
  exception, stays DRAFT.

Journal posting requires an approval_reference. 70,756 of 197,751
legacy POSTED journals carry no approver (system postings). The
transition stamps a provenance reference (`legacy:<source
table>:<id>`, the importer's source_key format) where the legacy
row itself is the approval record of truth; the stamped count is
receipt-visible and Gate 5 is blocked on a human ruling for this
family (see docs/rehearsal.md). Nothing is stamped silently.

Every failure becomes a named RowException (column="status");
the row keeps its last legal status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from erp_v2.importer.executor import RowException
from erp_v2.importer.spec import TransformSpec

_BATCH = 500

# destination table -> (initial status, {legacy status: path})
# A path is the sequence of destination statuses to walk through;
# None means "no module status" (named exception, stays initial).
_LIFECYCLES: dict[str, dict[str, tuple[str, ...] | None]] = {
    "mod_accounting.fiscal_periods": {
        "FUTURE": (),
        "OPEN": ("OPEN",),
        "SOFT_CLOSED": ("OPEN", "SOFT_CLOSED"),
    },
    "mod_accounting.journal_entries": {
        "DRAFT": (),
        "POSTED": ("POSTED",),
        "VOID": ("VOID",),
        "REVERSED": ("POSTED", "REVERSED"),
        "APPROVED": None,
        "SUBMITTED": None,
    },
    "mod_payables.supplier_invoices": {
        "DRAFT": (),
        "SUBMITTED": ("SUBMITTED",),
        "APPROVED": ("SUBMITTED", "APPROVED"),
        "POSTED": ("SUBMITTED", "APPROVED"),
        "PARTIALLY_PAID": ("SUBMITTED", "APPROVED"),
        "PAID": ("SUBMITTED", "APPROVED"),
        "VOID": ("VOID",),
        "REJECTED": None,
    },
}

# Extra SET clause applied on the journal DRAFT->POSTED step.
_JOURNAL_POSTED_SET = ", approval_reference = COALESCE(approval_reference, :approval_ref_prefix || CAST(id AS TEXT))"


@dataclass
class TransitionOutcome:
    destination_table: str
    transitioned: dict[str, int] = field(default_factory=dict)
    # destination status -> rows that reached it via this step
    stamped_approval_refs: int = 0
    exceptions: list[RowException] = field(default_factory=list)


def _update_batch(
    dest: sa.Connection,
    sql: sa.TextClause,
    rows: list[dict[str, Any]],
    spec: TransformSpec,
    outcome: TransitionOutcome,
    target_status: str,
) -> int:
    """Batched UPDATE with binary-split fallback; returns rows moved."""

    try:
        with dest.begin_nested():
            result = dest.execute(sql, rows)
        return int(result.rowcount or 0)
    except (sa.exc.IntegrityError, sa.exc.DataError) as exc:
        if len(rows) == 1:
            detail = str(exc.orig).splitlines()[0][:160] if exc.orig is not None else ""
            outcome.exceptions.append(
                RowException(
                    source_table=spec.source_table,
                    source_key=str(rows[0]["row_id"]),
                    column="status",
                    reason=f"transition to {target_status} rejected ({type(exc.orig).__name__}): {detail}",
                )
            )
            return 0
    midpoint = len(rows) // 2
    return _update_batch(dest, sql, rows[:midpoint], spec, outcome, target_status) + _update_batch(
        dest, sql, rows[midpoint:], spec, outcome, target_status
    )


def _run_family(
    source: sa.Connection,
    dest: sa.Connection,
    spec: TransformSpec,
    *,
    dry_run: bool,
) -> TransitionOutcome:
    paths = _LIFECYCLES[spec.destination_table]
    outcome = TransitionOutcome(destination_table=spec.destination_table)
    id_source = spec.column_map.get("id")
    if id_source is None:  # pragma: no cover - guarded by caller specs
        raise ValueError(f"{spec.destination_table}: spec does not map destination id")
    select_sql = sa.text(
        f"SELECT {id_source}, status FROM {spec.source_table}"  # noqa: S608
    )
    by_status: dict[str, list[Any]] = {}
    for row in source.execute(select_sql).mappings():
        by_status.setdefault(str(row["status"]), []).append(row[id_source])

    for legacy_status in sorted(by_status):
        ids = by_status[legacy_status]
        if legacy_status not in paths:
            for row_id in ids:
                outcome.exceptions.append(
                    RowException(
                        source_table=spec.source_table,
                        source_key=str(row_id),
                        column="status",
                        reason=f"legacy status {legacy_status!r} has no ruled module lifecycle mapping",
                    )
                )
            continue
        path = paths[legacy_status]
        if path is None:
            for row_id in ids:
                outcome.exceptions.append(
                    RowException(
                        source_table=spec.source_table,
                        source_key=str(row_id),
                        column="status",
                        reason=(
                            f"legacy status {legacy_status!r} has no module status; "
                            "row imported at the initial status for manual review"
                        ),
                    )
                )
            continue
        moving = list(ids)
        for target in path:
            if not moving:
                break
            if dry_run:
                outcome.transitioned[target] = outcome.transitioned.get(target, 0) + len(moving)
                continue
            extra = ""
            params_extra: dict[str, Any] = {}
            if spec.destination_table == "mod_accounting.journal_entries" and target == "POSTED":
                extra = _JOURNAL_POSTED_SET
                params_extra["approval_ref_prefix"] = f"legacy:{spec.source_table}:"
                # Count provenance stamps up front (rows whose mapped
                # approval_reference is NULL at posting time).
                # Chunked: psycopg caps one statement at 65535 params
                # and POSTED carries ~197k ids.
                for start in range(0, len(moving), 10000):
                    outcome.stamped_approval_refs += dest.execute(
                        sa.text(
                            f"SELECT count(*) FROM {spec.destination_table} "  # noqa: S608
                            "WHERE status = 'DRAFT' AND approval_reference IS NULL "
                            "AND id IN :ids"
                        ).bindparams(sa.bindparam("ids", expanding=True)),
                        {"ids": moving[start : start + 10000]},
                    ).scalar_one()
            sql = sa.text(
                f"UPDATE {spec.destination_table} SET status = :target_status{extra} "  # noqa: S608
                "WHERE id = :row_id AND status = :from_status"
            )
            from_status = (
                path[path.index(target) - 1]
                if path.index(target) > 0
                else ("FUTURE" if spec.destination_table == "mod_accounting.fiscal_periods" else "DRAFT")
            )
            moved = 0
            for start in range(0, len(moving), _BATCH):
                chunk = [
                    {"row_id": row_id, "target_status": target, "from_status": from_status, **params_extra}
                    for row_id in moving[start : start + _BATCH]
                ]
                moved += _update_batch(dest, sql, chunk, spec, outcome, target)
            outcome.transitioned[target] = outcome.transitioned.get(target, 0) + moved
            # Only rows that actually moved continue down the path.
            if moved < len(moving):
                still: list[Any] = []
                for start in range(0, len(moving), 10000):
                    still.extend(
                        dest.execute(
                            sa.text(
                                f"SELECT id FROM {spec.destination_table} "  # noqa: S608
                                "WHERE status = :target_status AND id IN :ids"
                            ).bindparams(sa.bindparam("ids", expanding=True)),
                            {
                                "target_status": target,
                                "ids": moving[start : start + 10000],
                            },
                        )
                        .scalars()
                        .all()
                    )
                moving = still
    return outcome


def run_transitions(
    source: sa.Connection,
    dest: sa.Connection,
    specs: tuple[TransformSpec, ...],
    *,
    dry_run: bool = False,
    commit: bool = False,
) -> tuple[TransitionOutcome, ...]:
    """Walk lifecycle-staged rows to their legacy final states.

    Runs after all specs (and mints) so every child row and ledger
    line already landed. Families run fiscal periods first, then
    journals, then payables.
    """

    by_dest: dict[str, TransformSpec] = {}
    for spec in specs:
        if spec.destination_table in _LIFECYCLES:
            by_dest[spec.destination_table] = spec
    outcomes: list[TransitionOutcome] = []
    for dest_table in (
        "mod_accounting.fiscal_periods",
        "mod_accounting.journal_entries",
        "mod_payables.supplier_invoices",
    ):
        spec = by_dest.get(dest_table)
        if spec is None:
            continue
        outcomes.append(_run_family(source, dest, spec, dry_run=dry_run))
        if commit and not dry_run:
            dest.commit()
    return tuple(outcomes)


__all__ = ["TransitionOutcome", "run_transitions"]
