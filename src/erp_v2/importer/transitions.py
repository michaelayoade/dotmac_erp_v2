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
legacy POSTED journals carry no approver (system postings).
Ruling (Michael, 2026-11-09): migrated journals need no approval
provenance - they post through the import path with their legacy
journal ID and posting date as the source. The transition stamps
`legacy:<source table>:<legacy journal id>@<posting date>`
(posting_date, falling back to entry_date) where no legacy
approval reference exists; the stamped count stays
receipt-visible. Nothing is stamped silently.

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
# Source = legacy journal ID (UUIDs are preserved, so `id` IS the
# legacy journal id) + posting date, per the 2026-11-09 ruling.
_JOURNAL_POSTED_SET = (
    ", approval_reference = COALESCE(approval_reference, "
    ":approval_ref_prefix || CAST(id AS TEXT) || '@' || "
    "CAST(COALESCE(posting_date, entry_date) AS TEXT))"
)


@dataclass
class TransitionOutcome:
    destination_table: str
    transitioned: dict[str, int] = field(default_factory=dict)
    # destination status -> rows that reached it via this step
    stamped_approval_refs: int = 0
    stamped_posted_by: int = 0
    recomputed_totals: int = 0
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
    commit: bool = False,
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

    # Journal REVERSED rows: the module trigger only accepts the
    # POSTED -> REVERSED update when reversal_journal_id changes
    # from NULL in the same statement. The importer's deferred
    # post-pass already populated it (while DRAFT), so stash and
    # clear it now, then re-apply it inside the REVERSED update.
    reversal_links: dict[Any, Any] = {}
    if spec.destination_table == "mod_accounting.journal_entries" and not dry_run:
        reversed_ids = by_status.get("REVERSED", [])
        for start in range(0, len(reversed_ids), 10000):
            chunk = reversed_ids[start : start + 10000]
            rows = dest.execute(
                sa.text(
                    f"SELECT id, reversal_journal_id FROM {spec.destination_table} "  # noqa: S608
                    "WHERE id IN :ids"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": chunk},
            )
            reversal_links.update({r[0]: r[1] for r in rows if r[1] is not None})
            dest.execute(
                sa.text(
                    f"UPDATE {spec.destination_table} SET reversal_journal_id = NULL "  # noqa: S608
                    "WHERE status = 'DRAFT' AND id IN :ids"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": chunk},
            )

    if spec.destination_table == "mod_accounting.journal_entries" and not dry_run:
        # Evidence completion (Gate 4 findings on production data):
        # 601 posted journals carry zero header totals in legacy
        # while their lines/ledger balance - the module trigger
        # compares ledger sums to the header, so recompute the
        # denormalized totals from the entry's own imported lines
        # (only where all four totals are zero and lines balance).
        # Still DRAFT at this point, so the edit is legal.
        # One set-based statement scoped to zero-total DRAFT
        # entries in the POSTED/REVERSED groups - the per-chunk
        # form re-aggregated all 478k lines per chunk (Gate 5
        # re-rehearsal: 40+ minutes). The id list is chunked only
        # for the parameter cap; the aggregate itself joins
        # through the scoped entries, so it stays small.
        scope_ids = by_status.get("POSTED", []) + by_status.get("REVERSED", [])
        for start in range(0, len(scope_ids), 60000):
            chunk = scope_ids[start : start + 60000]
            result = dest.execute(
                sa.text(
                    f"UPDATE {spec.destination_table} SET "  # noqa: S608
                    "total_debit = agg.d, total_credit = agg.c, "
                    "total_debit_functional = agg.fd, "
                    "total_credit_functional = agg.fc "
                    "FROM (SELECT jl.journal_id, sum(jl.debit) d, "
                    "sum(jl.credit) c, sum(jl.debit_functional) fd, "
                    "sum(jl.credit_functional) fc "
                    f"FROM mod_accounting.journal_lines jl "
                    f"JOIN {spec.destination_table} je0 ON je0.id = jl.journal_id "
                    "AND je0.status = 'DRAFT' AND je0.total_debit = 0 "
                    "AND je0.total_credit = 0 "
                    "AND je0.total_debit_functional = 0 "
                    "AND je0.total_credit_functional = 0 "
                    "AND je0.id IN :ids "
                    "GROUP BY jl.journal_id) agg "
                    f"WHERE agg.journal_id = {spec.destination_table}.id "
                    f"AND {spec.destination_table}.status = 'DRAFT' "
                    "AND agg.d = agg.c AND agg.fd = agg.fc AND agg.d > 0"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": chunk},
            )
            outcome.recomputed_totals += int(result.rowcount or 0)
        if commit:
            dest.commit()

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
            if spec.destination_table == "mod_accounting.journal_entries" and target == "REVERSED":
                extra = ", reversal_journal_id = :reversal_journal_id"
            if spec.destination_table == "mod_accounting.journal_entries" and target == "POSTED":
                # 206 legacy postings have no poster recorded (system
                # postings); the trigger requires posted_by/posted_at.
                # Stamp the provenance label, receipt-counted like the
                # approval references - same Gate 5 ruling family.
                for start in range(0, len(moving), 10000):
                    outcome.stamped_posted_by += dest.execute(
                        sa.text(
                            f"SELECT count(*) FROM {spec.destination_table} "  # noqa: S608
                            "WHERE status = 'DRAFT' AND posted_by IS NULL AND id IN :ids"
                        ).bindparams(sa.bindparam("ids", expanding=True)),
                        {"ids": moving[start : start + 10000]},
                    ).scalar_one()
                extra = _JOURNAL_POSTED_SET + (
                    ", posted_by = COALESCE(posted_by, 'legacy-system'), posted_at = COALESCE(posted_at, updated_at)"
                )
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
                    {
                        "row_id": row_id,
                        "target_status": target,
                        "from_status": from_status,
                        **params_extra,
                        **({"reversal_journal_id": reversal_links.get(row_id)} if target == "REVERSED" else {}),
                    }
                    for row_id in moving[start : start + _BATCH]
                ]
                moved += _update_batch(dest, sql, chunk, spec, outcome, target)
            outcome.transitioned[target] = outcome.transitioned.get(target, 0) + moved
            if commit and not dry_run:
                # Bounded transactions per (status group, step):
                # one mega-transaction over ~205k trigger-heavy
                # updates bloats the heap and pins a single cached
                # trigger plan for hours (Gate 4). Steps are
                # status-guarded UPDATEs - idempotent and resumable.
                dest.commit()
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
    if not dry_run and dest.dialect.name == "postgresql":
        # Fresh planner stats BEFORE the first trigger-heavy update:
        # autoanalyze ran while every journal was still DRAFT, so
        # the posting trigger's EXISTS/aggregate plans cached a
        # "POSTED is rare" estimate and scanned ~200k rows per
        # reversal (Gate 4: 60+ minutes in one family).
        for table in (
            "mod_accounting.journal_entries",
            "mod_accounting.journal_lines",
            "mod_accounting.posted_ledger_lines",
            "mod_accounting.fiscal_periods",
            "mod_payables.supplier_invoices",
        ):
            try:
                with dest.begin_nested():
                    dest.execute(sa.text(f"ANALYZE {table}"))
            except sa.exc.SQLAlchemyError:
                pass
        if commit:
            dest.commit()
    outcomes: list[TransitionOutcome] = []
    for dest_table in (
        "mod_accounting.fiscal_periods",
        "mod_accounting.journal_entries",
        "mod_payables.supplier_invoices",
    ):
        spec = by_dest.get(dest_table)
        if spec is None:
            continue
        outcomes.append(_run_family(source, dest, spec, dry_run=dry_run, commit=commit))
        if commit and not dry_run:
            dest.commit()
    return tuple(outcomes)


__all__ = ["TransitionOutcome", "run_transitions"]
