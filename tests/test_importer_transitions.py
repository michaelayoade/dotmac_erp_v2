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
