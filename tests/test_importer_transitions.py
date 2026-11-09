"""Lifecycle staging transitions (update mechanics; trigger legality
is proven against Postgres in the Gate 4 rehearsal)."""

from __future__ import annotations

import sqlalchemy as sa

from erp_v2.importer.spec import DestinationColumn, build_spec
from erp_v2.importer.transitions import run_transitions


def _fixture():
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE fiscal_period (fiscal_period_id TEXT, status TEXT)"))
        c.execute(
            sa.text(
                "INSERT INTO fiscal_period VALUES "
                "('p1','FUTURE'), ('p2','OPEN'), ('p3','SOFT_CLOSED'), ('p4','BIZARRE')"
            )
        )
    with dst.begin() as c:
        c.execute(sa.text("ATTACH ':memory:' AS mod_accounting"))
        c.execute(sa.text("CREATE TABLE mod_accounting.fiscal_periods (id TEXT PRIMARY KEY, status TEXT)"))
        c.execute(
            sa.text(
                "INSERT INTO mod_accounting.fiscal_periods VALUES "
                "('p1','FUTURE'), ('p2','FUTURE'), ('p3','FUTURE'), ('p4','FUTURE')"
            )
        )
    spec = build_spec(
        source_table="fiscal_period",
        destination_table="mod_accounting.fiscal_periods",
        source_columns=("fiscal_period_id", "status"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("status", nullable=False, has_default=False),
        ),
        renames={"id": "fiscal_period_id"},
        constants={"status": "FUTURE"},
    )
    assert "status" not in spec.column_map and spec.constants["status"] == "FUTURE"
    return src, dst, spec


def test_transitions_walk_paths_and_name_unruled_statuses() -> None:
    src, dst, spec = _fixture()
    with src.connect() as s, dst.begin() as d:
        (outcome,) = run_transitions(s, d, (spec,))
    assert outcome.transitioned == {"OPEN": 2, "SOFT_CLOSED": 1}
    assert len(outcome.exceptions) == 1
    assert outcome.exceptions[0].source_key == "p4"
    assert "BIZARRE" in outcome.exceptions[0].reason
    with dst.connect() as d:
        rows = dict(d.execute(sa.text("SELECT id, status FROM mod_accounting.fiscal_periods")).all())
    assert rows == {"p1": "FUTURE", "p2": "OPEN", "p3": "SOFT_CLOSED", "p4": "FUTURE"}


def test_transitions_dry_run_counts_without_writing() -> None:
    src, dst, spec = _fixture()
    with src.connect() as s, dst.connect() as d:
        (outcome,) = run_transitions(s, d, (spec,), dry_run=True)
    assert outcome.transitioned == {"OPEN": 2, "SOFT_CLOSED": 1}
    with dst.connect() as d:
        statuses = set(d.execute(sa.text("SELECT status FROM mod_accounting.fiscal_periods")).scalars())
    assert statuses == {"FUTURE"}


def test_journal_evidence_completion_recomputes_and_stamps() -> None:
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE journal_entry (journal_entry_id TEXT, status TEXT)"))
        c.execute(sa.text("INSERT INTO journal_entry VALUES ('j1','POSTED'), ('j2','POSTED'), ('j3','APPROVED')"))
    with dst.begin() as c:
        c.execute(sa.text("ATTACH ':memory:' AS mod_accounting"))
        c.execute(
            sa.text(
                "CREATE TABLE mod_accounting.journal_entries (id TEXT PRIMARY KEY, status TEXT, "
                "total_debit NUMERIC, total_credit NUMERIC, total_debit_functional NUMERIC, "
                "total_credit_functional NUMERIC, approval_reference TEXT, posted_by TEXT, "
                "posted_at TEXT, updated_at TEXT, reversal_journal_id TEXT, "
                "entry_date TEXT, posting_date TEXT)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO mod_accounting.journal_entries "
                "(id, status, total_debit, total_credit, total_debit_functional, "
                "total_credit_functional, updated_at, entry_date, posting_date) VALUES "
                "('j1','DRAFT',0,0,0,0,'2026-01-02','2026-01-10','2026-01-15'), "
                "('j2','DRAFT',50,50,50,50,'2026-01-02','2026-01-20',NULL), "
                "('j3','DRAFT',0,0,0,0,'2026-01-02','2026-01-25',NULL)"
            )
        )
        c.execute(
            sa.text(
                "CREATE TABLE mod_accounting.journal_lines "
                "(id TEXT, journal_id TEXT, debit NUMERIC, credit NUMERIC, "
                "debit_functional NUMERIC, credit_functional NUMERIC)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO mod_accounting.journal_lines VALUES "
                "('l1','j1',70,0,70,0), ('l2','j1',0,70,0,70), "
                "('l3','j3',5,0,5,0), ('l4','j3',0,5,0,5)"
            )
        )
    spec = build_spec(
        source_table="journal_entry",
        destination_table="mod_accounting.journal_entries",
        source_columns=("journal_entry_id", "status"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("status", nullable=False, has_default=False),
        ),
        renames={"id": "journal_entry_id"},
        constants={"status": "DRAFT"},
    )
    with src.connect() as s_, dst.begin() as d_:
        (outcome,) = run_transitions(s_, d_, (spec,))
    # j1 zero-totals recomputed and posted (poster + approval stamped);
    # j2 posted as-is (poster stamped only); j3 unruled, stays DRAFT.
    assert outcome.recomputed_totals == 1
    assert outcome.stamped_posted_by == 2
    assert outcome.stamped_approval_refs == 2
    assert outcome.transitioned == {"POSTED": 2}
    assert len(outcome.exceptions) == 1
    with dst.connect() as d_:
        rows = {
            r[0]: (r[1], float(r[2]), r[3], r[4])
            for r in d_.execute(
                sa.text(
                    "SELECT id, status, total_debit, posted_by, approval_reference FROM mod_accounting.journal_entries"
                )
            )
        }
    # Migrated journals need no approval provenance (ruling
    # 2026-11-09): the stamp is the legacy journal ID + posting
    # date as the source (entry_date when no posting_date).
    assert rows["j1"] == ("POSTED", 70.0, "legacy-system", "legacy:journal_entry:j1@2026-01-15")
    assert rows["j2"] == ("POSTED", 50.0, "legacy-system", "legacy:journal_entry:j2@2026-01-20")
    assert rows["j3"] == ("DRAFT", 0.0, None, None)
