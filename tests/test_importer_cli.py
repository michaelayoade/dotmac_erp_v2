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
    assert "e2_0011_leases" in data["destination_heads"]
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
