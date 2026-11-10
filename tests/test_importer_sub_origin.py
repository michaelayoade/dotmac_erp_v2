"""F1 part 2: Sub-origin rows stay out of the general importer."""

from __future__ import annotations

import sqlalchemy as sa

from erp_v2.importer.cli import _ALL_PAIRINGS, _sub_origin_receipt
from erp_v2.importer.executor import execute_transform
from erp_v2.importer.spec import DestinationColumn, build_spec
from erp_v2.importer.sub_origin import (
    LEGACY_SUB_ORIGIN_TABLES,
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
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, _cust_spec(policy=SubOriginPolicy.NOT_APPLICABLE))
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


def test_receipt_section_fails_closed_when_census_cannot_run() -> None:
    src, dst = _customer_engines()  # no invoice / payment tables: census cannot run
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, _cust_spec())
    section = _sub_origin_receipt(src, [], [report])
    assert section["specs"][0]["census"]["balanced"] is True
    assert section["specs"][0]["excluded_keys"] == ["c0", "c1"]
    assert "error" in section["customers_referenced_by_native"]
    good = _sub_origin_receipt(_census_engine(), [], [report])
    assert good["customers_referenced_by_native"]["keys"] == ["cA", "cZ"]


def test_receipt_section_skips_customer_census_when_no_exclude_specs() -> None:
    section = _sub_origin_receipt(sa.create_engine("sqlite://"), [], [])
    assert section["specs"] == []
    assert "skipped" in section["customers_referenced_by_native"]
