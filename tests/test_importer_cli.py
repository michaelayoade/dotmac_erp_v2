"""Operator CLI: census receipt, manifest binding, execute locks."""

from __future__ import annotations

import json

import pytest

from erp_v2.importer.cli import main
from erp_v2.importer.snapshot import ManifestError, SnapshotManifest

GOOD = {
    "snapshot_sha256": "a" * 64,
    "source_database": "legacy_snapshot_20261108",
    "destination_database": "erp_v2_candidate",
    "candidate_revision": "deadbeef",
    "tenant_extent": ["11111111-1111-1111-1111-111111111111"],
}


def _manifest(tmp_path, payload=GOOD):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload))
    return path


def test_census_receipt(tmp_path) -> None:
    receipt = tmp_path / "r.json"
    assert main(["census", "--receipt", str(receipt)]) == 0
    data = json.loads(receipt.read_text())
    assert data["legacy_tables_named"] == 426
    assert "e2_0012_academy_sync" in data["destination_heads"]
    assert data["pairings_total"] > 100


def test_manifest_validation(tmp_path) -> None:
    assert SnapshotManifest.load(_manifest(tmp_path)).tenant_extent
    bad = dict(GOOD, snapshot_sha256="xyz")
    with pytest.raises(ManifestError):
        SnapshotManifest.load(_manifest(tmp_path, bad))
    same = dict(GOOD, destination_database=GOOD["source_database"])
    with pytest.raises(ManifestError, match="differ"):
        SnapshotManifest.load(_manifest(tmp_path, same))


def test_plan_without_source_dsn_is_pairings_only(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LEGACY_DATABASE_URL", raising=False)
    receipt = tmp_path / "r.json"
    code = main(["plan", "--manifest", str(_manifest(tmp_path)), "--receipt", str(receipt)])
    assert code == 0
    data = json.loads(receipt.read_text())
    assert data["manifest"]["snapshot_sha256"] == "a" * 64
    assert "pairings-only" in data["mode"]


def test_execute_is_triple_locked(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ERP_IMPORT_APPROVED", raising=False)
    assert (
        main(
            [
                "execute",
                "--manifest",
                str(_manifest(tmp_path)),
                "--receipt",
                str(tmp_path / "r.json"),
            ]
        )
        == 2
    )
    assert main(["execute", "--receipt", str(tmp_path / "r.json")]) == 2


def test_billing_plan_receipt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ERP_SELLER_LEGAL_NAME", "Dotmac Ltd")
    monkeypatch.setenv("ERP_SELLER_ADDRESS_LINE", "1 Harbour Rd")
    invoices = tmp_path / "inv.json"
    invoices.write_text(
        json.dumps(
            [
                {
                    "invoice_id": "inv-1",
                    "invoice_number": "INV-1",
                    "customer_key": "cust-1",
                    "customer_legal_name": "Customer Ltd",
                    "customer_address_line": "2 Way",
                    "invoice_date": "2026-01-05T00:00:00+00:00",
                    "due_date": "2026-02-04",
                    "currency_code": "NGN",
                    "subtotal": "930.23",
                    "tax_amount": "69.77",
                    "total_amount": "1000.00",
                    "status": "posted",
                    "dotmac_sub_id": None,
                    "splynx_id": None,
                    "customer_dotmac_sub_id": None,
                },
                {
                    "invoice_id": "sub-1",
                    "invoice_number": "S-1",
                    "customer_key": "cust-1",
                    "invoice_date": "2026-01-05T00:00:00+00:00",
                    "currency_code": "NGN",
                    "total_amount": "500.00",
                    "status": "posted",
                    "dotmac_sub_id": "sub-ext-1",
                    "splynx_id": None,
                    "customer_dotmac_sub_id": None,
                },
            ]
        )
    )
    receipt = tmp_path / "r.json"
    code = main(
        [
            "billing-plan",
            "--manifest",
            str(_manifest(tmp_path)),
            "--invoices-json",
            str(invoices),
            "--source-account-key",
            "cust-1",
            "--account-id",
            "22222222-2222-2222-2222-222222222222",
            "--receipt",
            str(receipt),
        ]
    )
    assert code == 0
    data = json.loads(receipt.read_text())
    assert data["items"] == 1 and data["open_ar_minor"] == 100000
    assert data["sub_origin_exclusions"]["counts"]["invoices"] == 1
    assert data["sub_origin_exclusions"]["invoices"] == ["sub-1"]
    assert data["census"]["balanced"] is True and data["census"]["invoices"]["source"] == 2


def test_reflected_plan_survives_forbidden_runtime_env_names(tmp_path) -> None:
    """Regression (Gate 4 rehearsal): the CLI's default DSN env
    names are exactly the ones the runtime binding forbids; the CLI
    must take them out of the environment before importing the
    assembly, or every reflected plan/execute crashes."""

    import json as _json
    import os as _os
    import subprocess as _sp
    import sys as _sys

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        _json.dumps(
            {
                "snapshot_sha256": "a" * 64,
                "source_database": "legacy",
                "destination_database": "dest",
                "candidate_revision": "test",
                "tenant_extent": ["00000000-0000-0000-0000-000000000001"],
            }
        )
    )
    receipt = tmp_path / "receipt.json"
    env = {
        **_os.environ,
        "LEGACY_DATABASE_URL": "sqlite://",
        "MIGRATION_DATABASE_URL": "sqlite://",
    }
    env.pop("DATABASE_URL", None)
    proc = _sp.run(  # noqa: S603
        [
            _sys.executable,
            "-m",
            "erp_v2.importer",
            "plan",
            "--manifest",
            str(manifest),
            "--receipt",
            str(receipt),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = _json.loads(receipt.read_text())
    assert payload["mode"] == "reflected"
    assert payload["source_resolution"]["missing"], "empty sqlite must report missing sources"


def test_execute_run_constants_declared_supply() -> None:
    import argparse as _ap

    from erp_v2.importer.cli import _execute_run_constants
    from erp_v2.importer.snapshot import SnapshotManifest

    manifest = SnapshotManifest(
        snapshot_sha256="a" * 64,
        source_database="legacy",
        destination_database="dest",
        candidate_revision="rev1",
        tenant_extent=("00000000-0000-0000-0000-000000000001",),
    )
    args = _ap.Namespace(minor_units=2, source_owner="legacy-erp", source_version=None)
    constants = _execute_run_constants(args, manifest)
    assert constants["tenant_id"] == "00000000000000000000000000000001"  # CHAR(32) hex
    assert constants["minor_units"] == 2
    assert constants["source_owner"] == "legacy-erp"
    assert constants["source_version"] == "rev1"
    assert "created_by_id" not in constants  # no actor env in test process


def test_execute_apply_commits_per_spec(tmp_path, monkeypatch) -> None:
    """Regression (Gate 4): --apply commits per spec; wrapping the
    destination in engine.begin() made the first per-spec commit
    close the transaction context and every later statement raise
    'Can't operate on closed transaction inside context manager'.
    """
    import sqlalchemy as sa

    from erp_v2.importer import cli
    from erp_v2.importer.spec import DestinationColumn, build_spec

    src_path, dst_path = tmp_path / "src.db", tmp_path / "dst.db"
    src = sa.create_engine(f"sqlite:///{src_path}")
    dst = sa.create_engine(f"sqlite:///{dst_path}")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE widgets (widget_id TEXT, name TEXT)"))
        c.execute(sa.text("INSERT INTO widgets VALUES ('w1','One'), ('w2','Two')"))
        c.execute(sa.text("CREATE TABLE gadgets (gadget_id TEXT, name TEXT)"))
        c.execute(sa.text("INSERT INTO gadgets VALUES ('g1','Gizmo')"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE widgets_out (id TEXT, name TEXT)"))
        c.execute(sa.text("CREATE TABLE gadgets_out (id TEXT, name TEXT)"))

    def _spec(src_name, dst_name, pk):
        return build_spec(
            source_table=src_name,
            destination_table=dst_name,
            source_columns=(pk, "name"),
            destination_columns=(
                DestinationColumn("id", nullable=False, has_default=False),
                DestinationColumn("name", nullable=False, has_default=False),
            ),
            renames={"id": pk},
        )

    specs = (_spec("widgets", "widgets_out", "widget_id"), _spec("gadgets", "gadgets_out", "gadget_id"))
    monkeypatch.setattr(
        cli,
        "_resolve_sources",
        lambda engine, pairings: ({"widgets", "gadgets"}, {}, {}, ()),
    )
    monkeypatch.setattr(cli, "build_specs", lambda present, cols, dest: (specs, ()))
    monkeypatch.setenv("ERP_IMPORT_APPROVED", "1")
    monkeypatch.setenv("ERP_IMPORT_APPLY_OK", "1")
    monkeypatch.setenv("LEGACY_DATABASE_URL", f"sqlite:///{src_path}")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", f"sqlite:///{dst_path}")
    receipt = tmp_path / "r.json"
    code = main(["execute", "--manifest", str(_manifest(tmp_path)), "--receipt", str(receipt), "--apply"])
    assert code == 0
    data = json.loads(receipt.read_text())
    assert data["written"] == 3 and data["dry_run"] is False
    with dst.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM widgets_out")).scalar() == 2
        assert c.execute(sa.text("SELECT count(*) FROM gadgets_out")).scalar() == 1


def test_billing_preflight_wholly_sub_origin_is_a_clean_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ERP_SELLER_LEGAL_NAME", "Dotmac Ltd")
    invoices = tmp_path / "inv.json"
    invoices.write_text(
        json.dumps(
            [
                {
                    "invoice_id": "sub-1",
                    "invoice_number": "S-1",
                    "customer_key": "cust-1",
                    "invoice_date": "2026-01-05T00:00:00+00:00",
                    "currency_code": "NGN",
                    "total_amount": "500.00",
                    "status": "posted",
                    "dotmac_sub_id": "sub-ext-1",
                    "splynx_id": None,
                    "customer_dotmac_sub_id": None,
                }
            ]
        )
    )
    receipt = tmp_path / "r.json"
    code = main(
        [
            "billing-preflight",
            "--manifest",
            str(_manifest(tmp_path)),
            "--invoices-json",
            str(invoices),
            "--source-account-key",
            "cust-1",
            "--account-id",
            "22222222-2222-2222-2222-222222222222",
            "--expected-position-fingerprint",
            "fp",
            "--posting-watermark",
            "wm",
            "--migration-authority-ref",
            "ref",
            "--source-authority-ref",
            "ref",
            "--economic-cutoff",
            "2026-02-01T00:00:00+00:00",
            "--recorded-cutoff",
            "2026-02-01T00:00:00+00:00",
            "--source-schema-heads",
            "h1",
            "--receipt",
            str(receipt),
        ]
    )
    assert code == 0
    data = json.loads(receipt.read_text())
    assert data["nothing_to_import"] == "sub_origin"
    assert data["census"]["balanced"] is True
