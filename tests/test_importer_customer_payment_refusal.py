"""Historical AR payments cannot bypass confirmation evidence admission."""

import json
from contextlib import nullcontext
from dataclasses import replace

import pytest
import sqlalchemy as sa

from erp_v2.importer import cli
from erp_v2.importer.enrolment import MAP_BUILDERS, EnrolmentMaps, required_maps
from erp_v2.importer.executor import execute_transform
from erp_v2.importer.pipeline import run_import
from erp_v2.importer.spec import SourceRowRefusal, build_spec
from erp_v2.importer.sub_origin import SubOriginPolicy
from erp_v2.importer.waves36 import ALL_WAVE_PAIRINGS, build_specs

PAYMENT = next(p for p in ALL_WAVE_PAIRINGS if p.source == "public.customer_payment")
REASON = "historical_customer_payment_not_confirmation_evidence"
SOURCE_COLUMNS = ("payment_id", "customer_id", "organization_id", "dotmac_sub_id", "status", "amount")
NATIVE = [("n2", "c1", "tenant-b", None, "PENDING", None), ("n1", "c1", "tenant-a", None, "CLEARED", 100)]
SUB = [("s2", "c1", "tenant-b", "sub-2", "CLEARED", 200), ("s1", "c1", "tenant-a", "", "SENT", None)]
COHORTS = {"native": NATIVE, "sub": SUB, "mixed": SUB + NATIVE, "empty": []}


def _spec(columns=SOURCE_COLUMNS, policy=SubOriginPolicy.EXCLUDE):
    return build_spec(
        source_table="customer_payment",
        destination_table=PAYMENT.destination,
        source_columns=columns,
        destination_columns=(),
        source_row_refusal=PAYMENT.source_row_refusal,
        sub_origin=policy,
    )


def _source(engine, cohort):
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE customer_payment (payment_id TEXT, customer_id TEXT, organization_id TEXT, "
                "dotmac_sub_id TEXT, status TEXT, amount NUMERIC)"
            )
        )
        if COHORTS[cohort]:
            conn.execute(
                sa.text("INSERT INTO customer_payment VALUES (:id,:customer,:tenant,:tag,:status,:amount)"),
                [
                    dict(zip(("id", "customer", "tenant", "tag", "status", "amount"), row, strict=True))
                    for row in COHORTS[cohort]
                ],
            )
        # Real source tables used by the unchanged receipt customer census.
        conn.execute(sa.text("CREATE TABLE customer (customer_id TEXT, dotmac_sub_id TEXT)"))
        conn.execute(sa.text("INSERT INTO customer VALUES ('c1',NULL)"))
        conn.execute(sa.text("CREATE TABLE invoice (invoice_id TEXT, customer_id TEXT, dotmac_sub_id TEXT)"))
        conn.execute(sa.text("CREATE TABLE payment_intent (intent_id TEXT, customer_payment_id TEXT, status TEXT)"))
        conn.execute(sa.text("INSERT INTO payment_intent VALUES ('i1','n1','PENDING')"))


def test_customer_payment_pairing_refuses_without_financial_mappings():
    assert PAYMENT.source_row_refusal == SourceRowRefusal("payment_id", REASON)
    assert PAYMENT.sub_origin is SubOriginPolicy.EXCLUDE
    assert PAYMENT.renames == PAYMENT.constants == PAYMENT.lookups == {}
    assert "payment_intent_by_payment" not in MAP_BUILDERS
    table = sa.Table("payment_confirmations", sa.MetaData(), sa.Column("id", sa.String))
    specs, generated = build_specs((PAYMENT,), {PAYMENT.source: SOURCE_COLUMNS}, {PAYMENT.destination: table})
    assert not generated and not specs[0].executable
    assert specs[0].source_row_refusal == PAYMENT.source_row_refusal
    assert specs[0].sub_origin is SubOriginPolicy.EXCLUDE
    assert specs[0].column_map == {}
    assert specs[0].residue == tuple(sorted(SOURCE_COLUMNS))


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("cohort", COHORTS)
def test_source_policy_census_cannot_be_bypassed_by_maps_or_constants(tmp_path, dry_run, cohort):
    source, destination = sa.create_engine("sqlite://"), sa.create_engine("sqlite://")
    _source(source, cohort)
    with source.connect() as s, destination.begin() as d:
        d.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_payments"))
        d.execute(sa.text("CREATE TABLE mod_payments.payment_intents (id TEXT, status TEXT, amount NUMERIC)"))
        d.execute(sa.text("INSERT INTO mod_payments.payment_intents VALUES ('i1','PENDING',1)"))
        d.execute(sa.text("CREATE TABLE mod_payments.payment_confirmations (id TEXT, intent_id TEXT)"))
        calls = []
        sa.event.listen(d, "before_execute", lambda *args: calls.append(str(args[1])))
        tampered = replace(
            _spec(),
            run_supplied=("actor_id",),
            missing_required=("unprovided",),
            column_map={"id": "payment_id"},
            lookups={"intent_id": ("payment_intent_by_payment", "id", "intent_id")},
            constants={"source": "MANUAL"},
        )
        maps = EnrolmentMaps(s, d)
        assert required_maps((tampered,)) == frozenset()
        receipt = run_import(
            s,
            d,
            (tampered,),
            enrolment=maps,
            dry_run=dry_run,
            commit_per_spec=True,
            run_constants={"tenant_id": "tenant-a"},
            id_maps={"payment_intent_by_payment": {"n1": {"intent_id": "i1"}}},
        )
        report = receipt.reports[0]
        native = [r for r in COHORTS[cohort] if r[3] is None]
        excluded = sorted(r[0] for r in COHORTS[cohort] if r[3] is not None)
        assert report.read == len(native) and report.written == 0
        assert {e.source_key for e in report.exceptions} == {r[0] for r in native}
        assert {e.reason for e in report.exceptions} == ({REASON} if native else set())
        assert report.sub_origin_source == len(COHORTS[cohort])
        assert report.sub_origin_excluded_keys == tuple(excluded)
        assert report.census == {
            "source": len(COHORTS[cohort]),
            "imported": 0,
            "excluded": len(excluded),
            "refused": len(native),
            "balanced": True,
        }
        assert receipt.complete is (not native)
        assert maps.summary() == {} and calls == []
        assert d.execute(sa.text("SELECT count(*) FROM mod_payments.payment_confirmations")).scalar() == 0
        assert s.in_transaction() and not s.get_execution_options().get("stream_results")
        assert execute_transform(s, object(), _spec(), limit=1).read == len(native)
    section = cli._sub_origin_receipt(source, [], [report], tmp_path / "receipt.json")
    assert section["specs"][0]["census"] == report.census
    assert section["specs"][0]["excluded"]["count"] == len(excluded)
    side = json.loads((tmp_path / "receipt.json.sub-origin-keys.json").read_text())
    assert side[f"excluded:{PAYMENT.destination}"] == excluded
    assert section["incomplete"] == []


@pytest.mark.parametrize(
    "policy,columns,gap",
    [
        (SubOriginPolicy.EXCLUDE, ("payment_id",), "sub_origin_column:dotmac_sub_id"),
        (None, SOURCE_COLUMNS, "sub_origin_policy_undeclared:dotmac_sub_id"),
        (SubOriginPolicy.NOT_APPLICABLE, SOURCE_COLUMNS, "sub_origin_not_applicable_but_column_present:dotmac_sub_id"),
    ],
)
def test_refusal_preserves_sub_origin_validation(policy, columns, gap):
    spec = _spec(columns, policy)
    assert spec.sub_origin is policy
    assert gap in spec.missing_required
    report = execute_transform(object(), object(), spec)
    assert report.source_error == "source_row_refusal_invalid_sub_origin_policy"
    assert report.read == report.written == 0 and not report.complete


@pytest.mark.parametrize("shape", ["missing_table", "missing_tag", "missing_key"])
def test_unreadable_source_is_not_a_verified_empty_native_extent(shape):
    source = sa.create_engine("sqlite://")
    with source.begin() as conn:
        if shape != "missing_table":
            columns = "payment_id TEXT" if shape == "missing_tag" else "wrong_key TEXT, dotmac_sub_id TEXT"
            conn.execute(sa.text(f"CREATE TABLE customer_payment ({columns})"))
        # Reflected metadata going stale must also fail closed at execution.
        report = execute_transform(conn, object(), _spec())
        assert report.source_error == "source_row_refusal_census_unreadable"
        assert report.read == report.written == 0 and not report.complete
        assert conn.in_transaction() and conn.execute(sa.text("SELECT 1")).scalar() == 1


def test_partial_source_read_preserves_full_count_and_closes_cursor():
    class Rows:
        closed = False

        def __iter__(self):
            yield ("s1", "sub-1")
            yield ("n1", None)
            raise sa.exc.OperationalError("unreadable", {}, Exception("private payload"))

        def close(self):
            self.closed = True

    rows = Rows()
    calls = []

    class Source:
        def begin_nested(self):
            return nullcontext()

        def execute(self, sql, *, execution_options=None):
            calls.append(str(sql))
            if str(sql) == "SELECT count(*) FROM customer_payment":

                class Count:
                    def scalar_one(self):
                        return 9

                return Count()
            assert execution_options == {"stream_results": True, "yield_per": 1000}
            return rows

    report = execute_transform(Source(), object(), _spec())
    assert calls == ["SELECT count(*) FROM customer_payment", "SELECT payment_id, dotmac_sub_id FROM customer_payment"]
    assert rows.closed and report.read == 1 and report.written == 0
    assert report.sub_origin_source == 9 and report.sub_origin_excluded_keys == ("s1",)
    assert report.census == {"source": 9, "imported": 0, "excluded": 1, "refused": 1, "balanced": False}
    assert report.source_error == "source_row_refusal_census_unreadable" and not report.complete
    assert [(e.source_key, e.reason) for e in report.exceptions] == [("n1", REASON)]


@pytest.mark.parametrize("apply", [True, False])
@pytest.mark.parametrize("cohort", (*COHORTS, "missing_tag"))
def test_cli_receipt_preserves_real_sub_origin_census(tmp_path, monkeypatch, apply, cohort):
    source_url = f"sqlite:///{tmp_path / 'source.db'}"
    dest_url = f"sqlite:///{tmp_path / 'destination.db'}"
    source_engine = sa.create_engine(source_url)
    _source(source_engine, "empty" if cohort == "missing_tag" else cohort)
    if cohort == "missing_tag":
        with source_engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE customer_payment"))
            conn.execute(sa.text("CREATE TABLE customer_payment (payment_id TEXT, customer_id TEXT)"))
    monkeypatch.setattr(cli, "_ALL_PAIRINGS", (PAYMENT,))
    table = sa.Table("payment_confirmations", sa.MetaData(), sa.Column("id", sa.String))
    monkeypatch.setattr(cli, "_destination_tables", lambda: {PAYMENT.destination: table})
    monkeypatch.setenv("ERP_IMPORT_APPROVED", "1")
    monkeypatch.setenv("ERP_IMPORT_APPLY_OK", "1")
    monkeypatch.setenv("LEGACY_DATABASE_URL", source_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", dest_url)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "snapshot_sha256": "a" * 64,
                "source_database": "frozen_source",
                "destination_database": "candidate",
                "candidate_revision": "test",
                "tenant_extent": ["11111111-1111-1111-1111-111111111111"],
            }
        )
    )
    receipt_path = tmp_path / "receipt.json"
    argv = ["execute", "--manifest", str(manifest), "--receipt", str(receipt_path)]
    if apply:
        argv.append("--apply")
    rows = COHORTS.get(cohort, [])
    native = [r for r in rows if r[3] is None]
    excluded = sorted(r[0] for r in rows if r[3] is not None)
    assert cli.main(argv) == (3 if native or cohort == "missing_tag" else 0)
    receipt = json.loads(receipt_path.read_text())
    assert receipt["read"] == len(native) and receipt["written"] == 0
    assert receipt["complete"] is (not native and cohort != "missing_tag")
    assert receipt["sub_origin"]["incomplete"] == []
    if cohort == "missing_tag":
        assert receipt["source_row_refusals"][0]["source_error"] == "source_row_refusal_invalid_sub_origin_policy"
        assert receipt["sub_origin"]["specs"] == []
        return
    assert receipt["sub_origin"]["specs"][0]["census"] == {
        "source": len(COHORTS[cohort]),
        "imported": 0,
        "excluded": len(excluded),
        "refused": len(native),
        "balanced": True,
    }
    assert {e["source_key"] for e in receipt["exceptions"]} == {r[0] for r in native}
    assert {e["reason"] for e in receipt["exceptions"]} == ({REASON} if native else set())
    side = json.loads((tmp_path / "receipt.json.sub-origin-keys.json").read_text())
    assert side[f"excluded:{PAYMENT.destination}"] == excluded
