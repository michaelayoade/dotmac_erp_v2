"""Importer planner contract: order, refusal, and head binding."""

from __future__ import annotations

import pytest

from erp_v2.importer import WAVE1_MAPPINGS, PlanRefusal, TableMapping, build_plan
from erp_v2.migrate import EXPECTED_HEADS


def test_wave1_plan_blocked_by_unruled_numbering_enrolment() -> None:
    # Deliberate: the wave-1 mapping set must not plan until Michael's
    # numbering_sequence enrolment ruling lands. This test going stale
    # (plan builds) means the ruling was applied — update both together.
    with pytest.raises(PlanRefusal, match="numbering_sequence"):
        build_plan(WAVE1_MAPPINGS)


def test_plan_orders_dependencies_and_binds_expected_heads() -> None:
    mappings = (
        TableMapping(source="b", destination="mod_x.b", kind="carry", depends_on=("a",)),
        TableMapping(source="a", destination="mod_x.a", kind="carry"),
    )
    plan = build_plan(mappings)
    assert [s.source for s in plan.steps] == ["a", "b"]
    assert plan.destination_heads == tuple(sorted(EXPECTED_HEADS))


def test_wave_census_covers_every_legacy_table_once() -> None:
    from erp_v2.importer import SOURCE_TABLES, build_wave_mappings

    tables = [t for tables in SOURCE_TABLES.values() for t in tables]
    assert len(tables) == 426
    assert len(set(tables)) == 426
    # No wave beyond wave 1 can plan yet: every transform spec is pending.
    with pytest.raises(PlanRefusal):
        build_plan(build_wave_mappings())


def test_plan_refuses_cycles_duplicates_and_unknown_deps() -> None:
    with pytest.raises(PlanRefusal):
        build_plan(
            (
                TableMapping(source="a", destination="x", kind="carry", depends_on=("b",)),
                TableMapping(source="b", destination="x", kind="carry", depends_on=("a",)),
            )
        )
    with pytest.raises(PlanRefusal):
        build_plan(
            (
                TableMapping(source="a", destination="x", kind="carry"),
                TableMapping(source="a", destination="y", kind="carry"),
            )
        )
    with pytest.raises(PlanRefusal, match="unmapped"):
        build_plan((TableMapping(source="a", destination="x", kind="carry", depends_on=("zzz",)),))
