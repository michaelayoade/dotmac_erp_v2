"""ar_opening_cli: fail-closed boundary (no database needed)."""

from __future__ import annotations

import json
import stat
from uuid import UUID

from erp_v2.importer import ar_opening_cli as cli

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")


def _argv(command, tmp_path, *extra):
    return [
        command,
        "--manifest",
        str(tmp_path / "m.json"),
        "--snapshot-file",
        str(tmp_path / "s.bin"),
        "--receipt",
        str(tmp_path / "r.json"),
        "--cutoff",
        "2025-12-31T23:59:59+00:00",
        *extra,
    ]


def test_apply_and_mint_refuse_without_the_explicit_apply_flag(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ERP_AR_OPENING_APPLY", raising=False)
    for command in ("apply", "mint"):
        assert cli.main(_argv(command, tmp_path)) == 2
        assert not (tmp_path / "r.json").exists()  # refused before any receipt or database action
    assert "refused" in capsys.readouterr().out


def test_receipt_is_created_0600_and_never_overwritten(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LEGACY_DATABASE_URL", raising=False)
    assert cli.main(_argv("census", tmp_path)) == 2  # no operator database material
    receipt = tmp_path / "r.json"
    assert "refusal" in json.loads(receipt.read_text())
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert cli.main(_argv("census", tmp_path)) == 2  # the second run cannot clobber the first receipt


def test_plan_and_apply_need_authority_refs_and_apply_needs_the_fence(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ERP_AR_OPENING_APPLY", "1")
    assert cli.main(_argv("plan", tmp_path)) == 2
    assert json.loads((tmp_path / "r.json").read_text())["refusal"] == "authority_refs_required"
    (tmp_path / "r.json").unlink()
    code = cli.main(
        _argv("apply", tmp_path, "--migration-authority-ref", "dotmac-debt-register v1", "--source-authority-ref", "x")
    )
    assert code == 2 and json.loads((tmp_path / "r.json").read_text())["refusal"] == "coupled_watermark_required"


def test_customer_account_number_is_a_uuid5_not_the_legacy_id_and_is_stable() -> None:
    one = cli.customer_account_number(TENANT, "legacy-1")
    assert one == cli.customer_account_number(TENANT, "legacy-1")
    assert one != cli.customer_account_number(TENANT, "legacy-2") and "legacy" not in one.lower()
    assert len(one) <= 40


def test_plan_sha_binds_the_manifest_and_every_command_digest() -> None:
    a = cli.plan_sha256("f" * 64, {"1": "a" * 64, "2": "b" * 64})
    assert a == cli.plan_sha256("f" * 64, {"2": "b" * 64, "1": "a" * 64})
    assert a != cli.plan_sha256("e" * 64, {"1": "a" * 64, "2": "b" * 64})
    assert a != cli.plan_sha256("f" * 64, {"1": "a" * 64, "2": "c" * 64})
