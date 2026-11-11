"""Historical payment evidence cannot become settlement observations."""

from contextlib import nullcontext
from dataclasses import replace

import pytest
import sqlalchemy as sa

from erp_v2.importer.enrolment import MAP_BUILDERS, EnrolmentMaps, required_maps
from erp_v2.importer.executor import execute_transform
from erp_v2.importer.pipeline import partition_parent_blocked, run_import
from erp_v2.importer.spec import SourceRowRefusal, TransformSpec, build_spec
from erp_v2.importer.waves36 import ALL_WAVE_PAIRINGS, build_specs

PAYMENT = next(p for p in ALL_WAVE_PAIRINGS if p.source == "public.supplier_payment")
REASON = "historical_ap_payment_not_settlement_evidence"


def _spec():
    return build_spec(
        source_table="supplier_payment",
        destination_table="settlement_observations",
        source_columns=("payment_id", "organization_id", "status", "amount"),
        destination_columns=(),
        source_row_refusal=PAYMENT.source_row_refusal,
    )


def test_payment_pairing_preserves_source_without_insert_mappings():
    assert PAYMENT.source == "public.supplier_payment"
    assert PAYMENT.source_row_refusal == SourceRowRefusal("payment_id", REASON)
    assert not PAYMENT.lookups and not PAYMENT.renames
    table = sa.Table("settlement_observations", sa.MetaData(), sa.Column("id", sa.String))
    specs, generated = build_specs(
        (PAYMENT,), {PAYMENT.source: ("payment_id", "amount", "status")}, {PAYMENT.destination: table}
    )
    assert not generated
    assert not specs[0].executable
    assert specs[0].column_map == {}
    assert specs[0].residue == ("amount", "payment_id", "status")
    assert specs[0].source_row_refusal == PAYMENT.source_row_refusal
    assert "settlement_obligation" not in MAP_BUILDERS


@pytest.mark.parametrize("dry_run", [True, False])
def test_all_payments_refused_despite_obligations_maps_constants_and_filters(dry_run):
    src, dst = sa.create_engine("sqlite://"), sa.create_engine("sqlite://")
    with src.begin() as s, dst.begin() as d:
        s.execute(
            sa.text(
                "CREATE TABLE supplier_payment (payment_id TEXT, organization_id TEXT, status TEXT, amount NUMERIC)"
            )
        )
        s.execute(
            sa.text(
                "INSERT INTO supplier_payment VALUES "
                "('p1','tenant-a','SENT',100), ('p2','tenant-b','CLEARED',100), ('p3','tenant-a',NULL,NULL)"
            )
        )
        d.execute(sa.text("CREATE TABLE payment_obligations (id TEXT, invoice_id TEXT)"))
        d.execute(sa.text("INSERT INTO payment_obligations VALUES ('ob1','invoice1')"))
        d.execute(sa.text("CREATE TABLE settlement_observations (id TEXT, obligation_id TEXT)"))
        statements = []
        sa.event.listen(d, "before_execute", lambda *args: statements.append(str(args[1])))
        # Even a tampered insert-shaped spec must take refusal first.
        spec = replace(
            _spec(),
            missing_required=("anything",),
            run_supplied=("actor",),
            column_map={"id": "payment_id"},
            lookups={"obligation_id": ("settlement_obligation", "id", "obligation_id")},
        )
        assert not required_maps((spec,))
        maps = EnrolmentMaps(s, d)
        receipt = run_import(
            s,
            d,
            (spec,),
            enrolment=maps,
            id_maps={"settlement_obligation": {"p1": {"obligation_id": "ob1"}}},
            run_constants={"tenant_id": "tenant-a"},
            dry_run=dry_run,
        )
        assert receipt.read == 3 and receipt.written == 0 and not receipt.complete
        assert {e.source_key for e in receipt.exceptions} == {"p1", "p2", "p3"}
        assert {e.reason for e in receipt.exceptions} == {REASON}
        assert {e.column for e in receipt.exceptions} == {"payment_id"}
        assert maps.summary() == {} and statements == []
        assert d.execute(sa.text("SELECT count(*) FROM settlement_observations")).scalar() == 0
        # An executor limit cannot silently truncate a refusal census.
        assert execute_transform(s, d, spec, dry_run=dry_run, limit=1).read == 3


@pytest.mark.parametrize("shape", ["empty", "missing_table", "missing_key"])
def test_verified_empty_distinguished_from_unreadable_source(shape):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as s:
        if shape != "missing_table":
            column = "payment_id" if shape == "empty" else "wrong_key"
            s.execute(sa.text(f"CREATE TABLE supplier_payment ({column} TEXT)"))
        # No destination connection methods may be needed, even for errors.
        report = execute_transform(s, object(), _spec())
        assert report.read == report.written == 0
        assert report.complete is (shape == "empty")
        assert report.source_error == (None if shape == "empty" else "source_row_refusal_census_unreadable")
        assert s.in_transaction()
        assert s.execute(sa.text("SELECT 1")).scalar() == 1
        assert not s.get_execution_options().get("stream_results")


def test_partial_source_read_failure_is_incomplete_and_closes_cursor():
    class Rows:
        closed = False

        def __iter__(self):
            yield ("p1",)
            raise sa.exc.OperationalError("unreadable", {}, Exception("private payload"))

        def close(self):
            self.closed = True

    rows = Rows()

    class Source:
        def begin_nested(self):
            return nullcontext()

        def execute(self, sql, *, execution_options):
            assert str(sql) == "SELECT payment_id FROM supplier_payment"
            assert execution_options == {"stream_results": True, "yield_per": 1000}
            return rows

    report = execute_transform(Source(), object(), _spec())
    assert rows.closed
    assert report.read == 1 and report.written == 0 and not report.complete
    assert report.source_error == "source_row_refusal_census_unreadable"
    assert [(e.source_key, e.reason) for e in report.exceptions] == [("p1", REASON)]


def test_refusals_survive_parent_blocks_and_do_not_supply_children():
    metadata = sa.MetaData()
    parent = sa.Table("parents", metadata, sa.Column("id", sa.String, primary_key=True))
    settlement = sa.Table(
        "settlement_observations",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("parent_id", sa.ForeignKey(parent.c.id)),
    )
    child = sa.Table("children", metadata, sa.Column("id", sa.String), sa.Column("ref", sa.ForeignKey(settlement.c.id)))
    parent_spec = TransformSpec("parents_src", "parents", {}, ("blocked",), ())
    child_spec = TransformSpec("children_src", "children", {"id": "id"}, (), ())
    refusal = _spec()
    kept, skipped = partition_parent_blocked(
        (parent_spec, refusal, child_spec),
        (refusal, child_spec),
        {"parents": parent, "settlement_observations": settlement, "children": child},
    )
    assert kept == (refusal,)
    assert skipped == (child_spec,)


def test_source_refusal_rejects_unsafe_key():
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        SourceRowRefusal("payment_id; SELECT secret", REASON)
