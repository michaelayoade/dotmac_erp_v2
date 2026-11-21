"""ar_opening_cli: fail-closed boundary (no database needed)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

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


def test_the_cli_no_longer_creates_customers_or_numbers_them() -> None:
    assert not hasattr(cli, "customer_account_number") and not hasattr(cli, "mint_customers_and_accounts")
    source = (Path(cli.__file__)).read_text()
    assert "LEG-" not in source and "CustomerAccount(" not in source and "ALLOW_OPEN_VARIANCES" not in source


def test_non_uuid_legacy_customer_key_is_customer_not_imported() -> None:
    with pytest.raises(cli.OperatorRefusal, match="customer_not_imported"):
        cli.wave4_account_id(None, scope=SimpleNamespace(tenant_id=TENANT), legacy_customer_key="cust-1")


def test_window_dual_control_read_back_must_equal_the_policy() -> None:
    with pytest.raises(cli.OperatorRefusal, match="window_dual_control_differs_from_policy"):
        cli._verify_window_dual_control(SimpleNamespace(dual_control="off"), "on")
    cli._verify_window_dual_control(SimpleNamespace(dual_control="off"), "off")


def test_plan_sha_binds_the_manifest_and_every_command_digest() -> None:
    a = cli.plan_sha256("f" * 64, {"1": "a" * 64, "2": "b" * 64})
    assert a == cli.plan_sha256("f" * 64, {"2": "b" * 64, "1": "a" * 64})
    assert a != cli.plan_sha256("e" * 64, {"1": "a" * 64, "2": "b" * 64})
    assert a != cli.plan_sha256("f" * 64, {"1": "a" * 64, "2": "c" * 64})
    variance = [{"partition": "c", "source_key": "i", "basis": "stored_balance", "amount_minor": 5}]
    assert cli.plan_sha256("f" * 64, {"1": "a" * 64}, variance) != cli.plan_sha256("f" * 64, {"1": "a" * 64}, [])
    assert cli.plan_sha256("f" * 64, {}, [], "on") != cli.plan_sha256("f" * 64, {}, [], "off")  # dual_control is bound
    assert cli.plan_sha256("f" * 64, {}, [], "on", "a" * 64) != cli.plan_sha256("f" * 64, {}, [], "on", "b" * 64)


def test_attribution_sha_is_canonical_and_changes_with_any_edit():
    from erp_v2.importer.ar_attribution import attribution_sha256

    a = {"parties": [{"customer": "c", "gl_minor": 1}], "held": []}
    assert attribution_sha256(a) == attribution_sha256({"held": [], "parties": [{"gl_minor": 1, "customer": "c"}]})
    assert attribution_sha256(a) != attribution_sha256({"parties": [{"customer": "c", "gl_minor": 2}], "held": []})
