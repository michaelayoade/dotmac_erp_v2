"""Waves 3-6 pairing completeness + pipeline aggregation contract."""

from __future__ import annotations

import sqlalchemy as sa

from erp_v2.importer.pipeline import run_import
from erp_v2.importer.spec import DestinationColumn, build_spec
from erp_v2.importer.waves36 import (
    ALL_WAVE_PAIRINGS,
    WAVE3_PAIRINGS,
    WAVE4_PAIRINGS,
    WAVE56_PAIRINGS,
)


def test_every_billing_destination_is_command_import() -> None:
    billing = [p for p in WAVE4_PAIRINGS if p.destination.startswith("mod_billing.")]
    assert len(billing) == 35
    assert all(p.source is None for p in billing)


def test_no_destination_paired_to_conflicting_sources() -> None:
    by_dest: dict[str, set[str]] = {}
    for pairing in ALL_WAVE_PAIRINGS:
        if pairing.source:
            by_dest.setdefault(pairing.destination, set()).add(pairing.source)
    # Multi-source destinations are deliberate splits of one concept.
    multi = {d: s for d, s in by_dest.items() if len(s) > 1}
    assert set(multi) == {"mod_customers.customer_accounts"} or set(multi) <= {
        "mod_customers.customer_accounts",
        "mod_customers.customer_profiles",
    }


def test_wave_pairing_counts() -> None:
    assert len(WAVE3_PAIRINGS) == 73
    assert len(WAVE56_PAIRINGS) == 58
    assert len(ALL_WAVE_PAIRINGS) == len(WAVE3_PAIRINGS) + len(WAVE4_PAIRINGS) + len(WAVE56_PAIRINGS)


def test_pipeline_aggregates_reports_and_heads() -> None:
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    spec = build_spec(
        source_table="account",
        destination_table="accounts",
        source_columns=("account_id", "organization_id", "account_code", "account_name", "account_type"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("tenant_id", nullable=False, has_default=False),
            DestinationColumn("code", nullable=False, has_default=False),
            DestinationColumn("name", nullable=False, has_default=False),
            DestinationColumn("kind", nullable=False, has_default=False),
        ),
    )
    assert spec.executable
    with src.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE account (account_id TEXT, organization_id TEXT,"
                " account_code TEXT, account_name TEXT, account_type TEXT)"
            )
        )
        c.execute(sa.text("INSERT INTO account VALUES ('a1','t1','1000','Cash','asset')"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE accounts (id TEXT, tenant_id TEXT, code TEXT, name TEXT, kind TEXT)"))
    with src.connect() as s, dst.begin() as d:
        receipt = run_import(s, d, (spec,))
        assert receipt.complete
        assert (receipt.read, receipt.written) == (1, 1)
        assert "ac_0001_accounting" in receipt.destination_heads
        assert d.execute(sa.text("SELECT code, kind FROM accounts")).one() == (
            "1000",
            "asset",
        )
