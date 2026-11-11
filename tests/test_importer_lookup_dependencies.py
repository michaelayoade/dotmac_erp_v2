"""Hosted-CI regression coverage for destination-backed enrolment dependencies."""

from dataclasses import replace

import pytest
import sqlalchemy as sa

from erp_v2.importer.enrolment import MAP_BUILDERS, EnrolmentMaps, destination_dependencies
from erp_v2.importer.pipeline import order_specs, partition_parent_blocked, run_import
from erp_v2.importer.spec import SourceRowRefusal, TransformSpec
from erp_v2.importer.waves36 import ALL_WAVE_PAIRINGS, build_specs

ITEMS = "mod_inventory.items"
LINES = "mod_procurement.purchase_order_lines"


def _spec(destination, *, lookup=False, rewrite=False):
    return TransformSpec(
        source_table="source_rows",
        destination_table=destination,
        column_map={"id": "id"},
        missing_required=(),
        residue=(),
        lookups={"unit": ("inventory_item", "item_ref", "base_uom")} if lookup else {},
        fk_rewrites={"id": "inventory_item"} if rewrite else {},
    )


@pytest.mark.parametrize("rewrite", [False, True])
def test_map_dependencies_include_lookups_and_rewrites_without_fk_metadata(rewrite):
    consumer = _spec(LINES, lookup=not rewrite, rewrite=rewrite)
    producer = _spec(ITEMS)
    assert destination_dependencies((consumer,)) == {ITEMS}
    assert order_specs((consumer, producer), {}) == (producer, consumer)
    # A producer outside this batch is pre-existing/enrolment-owned.
    assert order_specs((consumer,), {}) == (consumer,)
    assert partition_parent_blocked((consumer,), (consumer,), {}) == ((consumer,), ())


@pytest.mark.parametrize("item_ref", ["item-1", "missing-item", None])
def test_real_pairings_import_items_before_first_cached_unit_lookup(item_ref):
    src_engine, dst_engine = sa.create_engine("sqlite://"), sa.create_engine("sqlite://")
    with src_engine.begin() as src, dst_engine.begin() as dst:
        src.execute(sa.text("ATTACH DATABASE ':memory:' AS public"))
        for schema in ("mod_inventory", "mod_procurement"):
            dst.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        src.execute(sa.text("CREATE TABLE public.item (id TEXT, base_uom TEXT)"))
        src.execute(sa.text("INSERT INTO public.item VALUES ('item-1', 'kg')"))
        # No source unit column: inventory base_uom is the declared authority.
        src.execute(sa.text("CREATE TABLE public.purchase_order_line (id TEXT, item_ref TEXT)"))
        src.execute(
            sa.text("INSERT INTO public.purchase_order_line VALUES ('line-1', :item_ref)"), {"item_ref": item_ref}
        )
        metadata = sa.MetaData()
        items = sa.Table(
            "items",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("base_uom", sa.String),
            schema="mod_inventory",
        )
        lines = sa.Table(
            "purchase_order_lines",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("item_ref", sa.String(255)),
            sa.Column("unit", sa.String, nullable=False),
            schema="mod_procurement",
        )
        assert not lines.foreign_keys
        metadata.create_all(dst)
        tables = {ITEMS: items, LINES: lines}
        pairings = tuple(next(p for p in ALL_WAVE_PAIRINGS if p.destination == name) for name in (LINES, ITEMS))
        specs, generated = build_specs(
            pairings, {"public.item": ("id", "base_uom"), "public.purchase_order_line": ("id", "item_ref")}, tables
        )
        assert not generated and all(spec.executable for spec in specs)
        ordered = order_specs(specs, tables)
        assert tuple(spec.destination_table for spec in ordered) == (ITEMS, LINES)
        maps = EnrolmentMaps(src, dst)
        receipt = run_import(src, dst, ordered, enrolment=maps)
        assert receipt.enrolment_maps == {"inventory_item": 1}
        assert maps.get("inventory_item") == {"item-1": {"base_uom": "kg"}}
        assert receipt.reports[0].written == 1
        if item_ref == "item-1":
            assert receipt.complete and receipt.written == 2
            assert dst.execute(sa.select(lines.c.unit)).scalar_one() == "kg"
        else:
            assert not receipt.complete and receipt.written == 1
            assert dst.execute(sa.select(sa.func.count()).select_from(lines)).scalar_one() == 0
            assert len(receipt.exceptions) == 1
            exception = receipt.exceptions[0]
            assert exception.source_key == "line-1" and exception.column == "unit"
            assert exception.reason == "no inventory_item lookup entry field 'base_uom' for item_ref value"


def test_blocked_map_producer_blocks_consumers_and_fk_descendants():
    metadata = sa.MetaData()
    lines = sa.Table(
        "purchase_order_lines", metadata, sa.Column("id", sa.String, primary_key=True), schema="mod_procurement"
    )
    children = sa.Table(
        "children", metadata, sa.Column("line_id", sa.String, sa.ForeignKey(f"{LINES}.id")), schema="mod_procurement"
    )
    producer, consumer, child = _spec(ITEMS), _spec(LINES, lookup=True), _spec("mod_procurement.children")
    all_specs = (child, consumer, producer)
    tables = {LINES: lines, child.destination_table: children}
    assert partition_parent_blocked(all_specs, (child, consumer), tables) == ((), (child, consumer))
    assert partition_parent_blocked(all_specs, all_specs, tables) == (all_specs, ())


def test_map_cycle_fails_instead_of_running_a_consumer_before_its_producer():
    # A producer reading its own map cannot safely construct that cached map.
    with pytest.raises(ValueError, match="cannot order import destination-map dependencies"):
        order_specs((_spec(ITEMS, lookup=True),), {})
    # Physical-FK back edges must not trigger the old unsafe cycle fallback.
    metadata = sa.MetaData()
    lines = sa.Table(
        "purchase_order_lines", metadata, sa.Column("id", sa.String, primary_key=True), schema="mod_procurement"
    )
    items = sa.Table(
        "items", metadata, sa.Column("line_id", sa.String, sa.ForeignKey(f"{LINES}.id")), schema="mod_inventory"
    )
    with pytest.raises(ValueError, match="cannot order import destination-map dependencies"):
        order_specs((_spec(LINES, lookup=True), _spec(ITEMS)), {LINES: lines, ITEMS: items})


def test_fk_only_cycles_preserve_declaration_order():
    metadata = sa.MetaData()
    a = sa.Table(
        "a", metadata, sa.Column("id", sa.String, primary_key=True), sa.Column("b_id", sa.String, sa.ForeignKey("b.id"))
    )
    b = sa.Table(
        "b", metadata, sa.Column("id", sa.String, primary_key=True), sa.Column("a_id", sa.String, sa.ForeignKey("a.id"))
    )
    specs = (_spec("b"), _spec("a"))
    assert order_specs(specs, {"a": a, "b": b}) == specs


def test_source_refusal_never_requires_or_builds_map_dependencies(monkeypatch):
    def unexpected_builder(*args):
        pytest.fail("source refusal tried to build a destination map")

    monkeypatch.setitem(MAP_BUILDERS, "inventory_item", unexpected_builder)
    refusal = replace(_spec(LINES, lookup=True), source_row_refusal=SourceRowRefusal("id", "source_evidence_only"))
    assert not destination_dependencies((refusal,))
    assert order_specs((refusal, _spec(ITEMS)), {}) == (refusal, _spec(ITEMS))
    assert partition_parent_blocked((refusal, _spec(ITEMS)), (refusal,), {}) == ((refusal,), ())
    src_engine, dst_engine = sa.create_engine("sqlite://"), sa.create_engine("sqlite://")
    with src_engine.begin() as src, dst_engine.begin() as dst:
        src.execute(sa.text("CREATE TABLE source_rows (id TEXT)"))
        src.execute(sa.text("INSERT INTO source_rows VALUES ('evidence-1')"))
        statements = []
        sa.event.listen(dst, "before_execute", lambda *args: statements.append(str(args[1])))
        maps = EnrolmentMaps(src, dst)
        receipt = run_import(src, dst, (refusal,), enrolment=maps)
        assert receipt.read == 1 and receipt.written == 0
        assert receipt.exceptions[0].reason == "source_evidence_only"
        assert not maps.summary() and not statements
