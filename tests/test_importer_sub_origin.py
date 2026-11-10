"""F1 part 2: Sub-origin rows stay out of the general importer."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import sqlalchemy as sa

from erp_v2.importer.cli import _ALL_PAIRINGS, _sub_origin_receipt
from erp_v2.importer.executor import execute_transform
from erp_v2.importer.spec import DestinationColumn, build_spec
from erp_v2.importer.sub_origin import (
    LEGACY_SUB_ORIGIN_TABLES,
    SUB_CUSTOMER_REFERENCE_REASON,
    SubCustomerRef,
    SubOriginPolicy,
    is_sub_origin_table,
    sub_tagged_customers_referenced_by_native,
)

# Pairings whose source is a tagged legacy table, with the policy chosen.
EXPECTED_POLICIES = {
    ("mod_payments.payment_confirmations", "public.customer_payment"): SubOriginPolicy.EXCLUDE,
    ("mod_customers.customer_accounts", "public.customer"): SubOriginPolicy.EXCLUDE,
    ("mod_customers.customer_profiles", "public.customer"): SubOriginPolicy.EXCLUDE,
}


def test_every_pairing_over_a_tagged_table_declares_a_policy() -> None:
    tagged = [p for p in _ALL_PAIRINGS if is_sub_origin_table(p.source)]
    assert tagged, "the structural test must see the tagged pairings"
    undeclared = [(p.destination, p.source) for p in tagged if p.sub_origin is None]
    assert undeclared == []
    assert {(p.destination, p.source): p.sub_origin for p in tagged} == EXPECTED_POLICIES


def test_policies_are_only_declared_where_they_make_sense() -> None:
    for pairing in _ALL_PAIRINGS:
        if pairing.sub_origin is SubOriginPolicy.EXCLUDE:
            assert is_sub_origin_table(pairing.source), pairing.destination
        if pairing.sub_origin is SubOriginPolicy.NOT_APPLICABLE:
            assert not is_sub_origin_table(pairing.source), pairing.destination


def test_tagged_table_constant_is_the_audited_legacy_list() -> None:
    assert LEGACY_SUB_ORIGIN_TABLES == {"customer", "customer_payment", "invoice"}
    assert is_sub_origin_table("ar.customer") and not is_sub_origin_table("ar.customer_group")


def _cust_spec(columns=("customer_id", "name", "dotmac_sub_id"), policy=SubOriginPolicy.EXCLUDE):
    return build_spec(
        source_table="customer",
        destination_table="accounts",
        source_columns=columns,
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("name", nullable=False, has_default=False),
        ),
        renames={"id": "customer_id"},
        sub_origin=policy,
    )


def test_spec_refuses_exclude_without_column_and_column_without_policy() -> None:
    no_col = _cust_spec(columns=("customer_id", "name"))
    assert not no_col.executable and "sub_origin_column:dotmac_sub_id" in no_col.missing_required
    undeclared = _cust_spec(policy=None)
    assert not undeclared.executable and "sub_origin_policy_undeclared:dotmac_sub_id" in undeclared.missing_required
    assert _cust_spec().executable
    assert _cust_spec(columns=("customer_id", "name"), policy=SubOriginPolicy.NOT_APPLICABLE).executable


def _customer_engines():
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE customer (customer_id TEXT, name TEXT, dotmac_sub_id TEXT)"))
        c.execute(
            sa.text(
                "INSERT INTO customer VALUES ('c3','Native C',NULL),('c1','Sub A','sub-1'),"
                "('c2','Native B',NULL),('c0','Blank','')"
            )
        )
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE accounts (id TEXT, name TEXT)"))
    return src, dst


def test_executor_excludes_sub_tagged_rows_and_reconciles_census() -> None:
    src, dst = _customer_engines()
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, _cust_spec())
        assert sorted(r[0] for r in d.execute(sa.text("SELECT id FROM accounts"))) == ["c2", "c3"]
    assert report.census == {"source": 4, "imported": 2, "excluded": 2, "refused": 0, "balanced": True}
    assert report.sub_origin_excluded_keys == ("c0", "c1")  # blank string counts; sorted
    assert report.complete


def test_executor_dry_run_census_and_refusals_counted() -> None:
    src, dst = _customer_engines()
    spec = build_spec(
        source_table="customer",
        destination_table="accounts",
        source_columns=("customer_id", "name", "dotmac_sub_id"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("name", nullable=False, has_default=False),
        ),
        renames={"id": "customer_id"},
        fk_rewrites={"id": "ids"},
        sub_origin=SubOriginPolicy.EXCLUDE,
    )
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec, id_maps={"ids": {"c2": "C2"}}, dry_run=True)
    assert report.census == {"source": 4, "imported": 1, "excluded": 2, "refused": 1, "balanced": True}
    assert not report.complete  # a refusal is still a refusal


def test_executor_refuses_exclude_when_source_lacks_the_column() -> None:
    import pytest

    src, dst = _customer_engines()
    with src.connect() as s, dst.begin() as d, pytest.raises(ValueError, match="sub_origin_column"):
        execute_transform(s, d, _cust_spec(columns=("customer_id", "name")))


def test_non_exclude_specs_have_no_census() -> None:
    src, dst = _customer_engines()
    spec = _cust_spec(columns=("customer_id", "name"), policy=SubOriginPolicy.NOT_APPLICABLE)
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec)
    assert report.census is None and report.read == 4


def _census_engine():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        for ddl in (
            "CREATE TABLE customer (customer_id TEXT, dotmac_sub_id TEXT)",
            "CREATE TABLE invoice (invoice_id TEXT, customer_id TEXT, dotmac_sub_id TEXT)",
            "CREATE TABLE customer_payment (payment_id TEXT, customer_id TEXT, dotmac_sub_id TEXT)",
            "INSERT INTO customer VALUES ('cB','s2'),('cA','s1'),('cN',NULL),('cZ','s3')",
            "INSERT INTO invoice VALUES ('i1','cA',NULL),('i2','cB','sub-inv'),('i3','cN',NULL)",
            "INSERT INTO customer_payment VALUES ('p1','cZ',NULL),('p2','cB','sub-pay')",
        ):
            c.execute(sa.text(ddl))
    return engine


def test_sub_tagged_customers_referenced_by_native_rows_are_reported_sorted() -> None:
    engine = _census_engine()
    with engine.connect() as c:
        census = sub_tagged_customers_referenced_by_native(c)
    assert census["keys"] == ["cA", "cZ"]  # cB is referenced only by Sub-origin rows
    assert census["via_invoices"] == ["cA"] and census["via_payments"] == ["cZ"]
    assert census["count"] == 2 and census["imported"] is False


def test_not_applicable_is_refused_when_the_source_has_the_column() -> None:
    spec = _cust_spec(policy=SubOriginPolicy.NOT_APPLICABLE)
    assert not spec.executable
    assert "sub_origin_not_applicable_but_column_present:dotmac_sub_id" in spec.missing_required


def _run_customer_report():
    src, dst = _customer_engines()
    with src.connect() as s, dst.begin() as d:
        return execute_transform(s, d, _cust_spec())


def test_receipt_writes_full_keys_to_mode_600_side_file(tmp_path) -> None:
    report = _run_customer_report()
    receipt = tmp_path / "r.json"
    section = _sub_origin_receipt(_census_engine(), [], [report], receipt)
    side = tmp_path / "r.json.sub-origin-keys.json"
    assert stat.S_IMODE(os.stat(side).st_mode) == 0o600
    data = json.loads(side.read_text())
    assert data["excluded:accounts"] == ["c0", "c1"]
    assert data["customers_referenced_by_native"] == ["cA", "cZ"]
    evidence = section["specs"][0]["excluded"]
    assert evidence == {
        "count": 2,
        "keys_sha256": hashlib.sha256(b"c0\nc1").hexdigest(),
        "keys_file": side.name,
    }
    assert "keys_truncated" not in json.dumps(section) and "excluded_keys" not in json.dumps(section)
    assert section["customers_referenced_by_native"]["keys_file"] == side.name
    assert section["incomplete"] == []


def test_side_file_failure_makes_the_run_incomplete(tmp_path) -> None:
    report = _run_customer_report()
    section = _sub_origin_receipt(_census_engine(), [], [report], tmp_path / "missing-dir" / "r.json")
    assert any("side file could not be written" in r for r in section["incomplete"])


def test_receipt_fails_closed_when_census_cannot_run(tmp_path) -> None:
    src, _dst = _customer_engines()  # no invoice / payment tables
    report = _run_customer_report()
    section = _sub_origin_receipt(src, [], [report], tmp_path / "r.json")
    assert "error" in section["customers_referenced_by_native"]
    assert any("census could not run" in r for r in section["incomplete"])


def test_receipt_section_skips_customer_census_when_no_exclude_specs(tmp_path) -> None:
    section = _sub_origin_receipt(sa.create_engine("sqlite://"), [], [], tmp_path / "r.json")
    assert section["specs"] == [] and section["incomplete"] == []
    assert "skipped" in section["customers_referenced_by_native"]


def _payment_spec():
    return build_spec(
        source_table="customer_payment",
        destination_table="confirmations",
        source_columns=("payment_id", "customer_id", "dotmac_sub_id"),
        destination_columns=(DestinationColumn("id", nullable=False, has_default=False),),
        renames={"id": "payment_id"},
        sub_origin=SubOriginPolicy.EXCLUDE,
    )


def test_payment_intents_on_sub_tagged_payments_make_the_run_incomplete(tmp_path) -> None:
    engine = _census_engine()
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE payment_intent (intent_id TEXT, customer_payment_id TEXT)"))
        c.execute(sa.text("INSERT INTO payment_intent VALUES ('in1','p2'),('in2','p1'),('in3',NULL)"))
    dst = sa.create_engine("sqlite://")
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE confirmations (id TEXT)"))
    with engine.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, _payment_spec())
    section = _sub_origin_receipt(engine, [], [report], tmp_path / "r.json")
    assert section["payment_intents_on_sub_payments"]["count"] == 1  # in1 -> p2 (Sub-tagged)
    assert "payment_intent rows reference Sub-tagged customer_payment (routing pending)" in section["incomplete"]
    assert json.loads((tmp_path / "r.json.sub-origin-keys.json").read_text())["payment_intents_on_sub_payments"] == [
        "in1"
    ]


def _ref_engines():
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        for ddl in (
            "CREATE TABLE customer (customer_id TEXT, dotmac_sub_id TEXT)",
            "INSERT INTO customer VALUES ('cN',NULL),('cS','sub-1')",
            "CREATE TABLE quote (quote_id TEXT, customer_id TEXT)",
            "INSERT INTO quote VALUES ('q1','cN'),('q2','cS')",
            "CREATE TABLE quote_line (line_id TEXT, quote_id TEXT)",
            "INSERT INTO quote_line VALUES ('l1','q1'),('l2','q2'),('l3','q2')",
            "CREATE TABLE contract (contract_id TEXT, customer_id TEXT)",
            "INSERT INTO contract VALUES ('k1','cN'),('k2','cS'),('k3',NULL)",
        ):
            c.execute(sa.text(ddl))
    with dst.begin() as c:
        for t in ("quotes", "quote_lines", "agreements"):
            c.execute(sa.text(f"CREATE TABLE {t} (id TEXT)"))
    return src, dst


def _ref_spec(source, dest, key, ref):
    return build_spec(
        source_table=source,
        destination_table=dest,
        source_columns=(key,),
        destination_columns=(DestinationColumn("id", nullable=False, has_default=False),),
        renames={"id": key},
        sub_customer_ref=ref,
    )


def test_children_referencing_sub_tagged_customers_become_named_exceptions() -> None:
    src, dst = _ref_engines()
    cases = (
        ("contract", "agreements", "contract_id", SubCustomerRef("customer_id"), ["k2"], 2),
        ("quote", "quotes", "quote_id", SubCustomerRef("customer_id"), ["q2"], 1),
        (
            "quote_line",
            "quote_lines",
            "line_id",
            SubCustomerRef("quote_id", via_table="quote", via_key="quote_id", via_customer="customer_id"),
            ["l2", "l3"],
            1,
        ),
    )
    for source, dest, key, ref, refused, written in cases:
        with src.connect() as s, dst.begin() as d:
            report = execute_transform(s, d, _ref_spec(source, dest, key, ref))
        assert list(report.sub_customer_ref_rows) == refused, source
        assert {e.reason for e in report.exceptions} == {SUB_CUSTOMER_REFERENCE_REASON}
        assert report.written == written and not report.complete


def test_child_pairings_declare_the_customer_reference() -> None:
    by_dest = {p.destination: p for p in _ALL_PAIRINGS}
    assert by_dest["mod_agreements.agreements"].sub_customer_ref == SubCustomerRef("customer_id")
    assert by_dest["mod_sales.quotes"].sub_customer_ref == SubCustomerRef("customer_id")
    assert by_dest["mod_sales.quote_lines"].sub_customer_ref.via_table == "quote"
