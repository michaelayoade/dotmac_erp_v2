"""B1b recognize-and-post operator CLI: list format, receipt shape and authorization gates (no database)."""

import hashlib
import json
import os
from decimal import Decimal
from uuid import UUID

import pytest

from erp_v2.importer.snapshot import SnapshotManifest
from erp_v2.legacy_ap import recognize_cli as cli

TENANT, INVOICE = str(UUID(int=1)), str(UUID(int=2))
REVISION, SNAP = "a" * 40, "b" * 64


def manifest():
    return SnapshotManifest(SNAP, "source", "destination", REVISION, (TENANT,))


def listing(**change):
    base = dict(
        version=1,
        tenant_id=TENANT,
        census_ref="step3-census",
        snapshot_sha256=SNAP,
        candidate_revision=REVISION,
        invoice_ids=[INVOICE],
    )
    return base | change


def encode(data) -> tuple[bytes, str]:
    raw = json.dumps(data).encode()
    return raw, hashlib.sha256(raw).hexdigest()


def test_valid_list_parses_and_sorts():
    other = str(UUID(int=1 << 100))
    raw, sha = encode(listing(invoice_ids=[other, INVOICE]))
    tenant, ref, ids = cli.parse_list(raw, sha, manifest())
    assert (str(tenant), ref) == (TENANT, "step3-census")
    assert [str(i) for i in ids] == sorted([INVOICE, other])


def test_digest_is_mandatory_and_must_match():
    raw, sha = encode(listing())
    with pytest.raises(cli.RecognizeRefusal, match="list_sha256_required"):
        cli.parse_list(raw, "", manifest())
    with pytest.raises(cli.RecognizeRefusal, match="list_digest_mismatch"):
        cli.parse_list(raw + b" ", sha, manifest())


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"version": True}, "invalid_list_version"),
        ({"version": 2}, "invalid_list_version"),
        ({"invoice_ids": []}, "explicit_bounded_list_required"),
        ({"invoice_ids": [INVOICE, INVOICE]}, "duplicate_invoice_id"),
        ({"invoice_ids": ["not-a-uuid"]}, "invalid_invoice_id"),
        ({"invoice_ids": [str(UUID(int=0xABC)).upper()]}, "invalid_invoice_id"),
        ({"invoice_ids": [str(UUID(int=n)) for n in range(1, 1002)]}, "explicit_bounded_list_required"),
        ({"tenant_id": str(UUID(int=9))}, "tenant_extent_mismatch"),
        ({"snapshot_sha256": "c" * 64}, "list_snapshot_mismatch"),
        ({"candidate_revision": "d" * 40}, "list_candidate_mismatch"),
        ({"census_ref": " padded "}, "census_ref_required"),
        ({"extra": 1}, "invalid_list_schema"),
    ],
)
def test_list_bindings(change, reason):
    raw, sha = encode(listing(**change))
    with pytest.raises(cli.RecognizeRefusal, match=reason):
        cli.parse_list(raw, sha, manifest())


def test_strict_json_refuses_duplicate_keys():
    raw = b'{"version":1,"version":2}'
    with pytest.raises(cli.RecognizeRefusal, match="duplicate_json_key"):
        cli.parse_list(raw, hashlib.sha256(raw).hexdigest(), manifest())


def test_references_bind_the_list_digest_and_actor():
    assert cli.evidence_reference("c" * 64) == "erp.b1b.list:" + "c" * 64
    assert cli.approval_reference("c" * 64, "actor-1").endswith(":approver:actor-1")
    assert len(cli.approval_reference("c" * 64, str(UUID(int=7)))) < 255


def plan():
    rows = [
        dict(
            invoice_id=str(UUID(int=n)),
            evidence_fingerprint="cv1:" + "e" * 64,
            subtotal="200.00",
            tax="15.00",
            total="215.00",
        )
        for n in (10, 11)
    ]
    return dict(
        version=1,
        bindings=dict(list_sha256="c" * 64),
        eligible=rows,
        already_done=[dict(invoice_id=str(UUID(int=12)))],
        refused=[dict(invoice_id=str(UUID(int=13)), reason="not_approved")],
        digest="f" * 64,
    )


def test_receipt_holds_counts_and_totals_only():
    receipt = cli.receipt_of(plan(), mode="apply", applied=2, posted_total=Decimal("430"))
    assert receipt["counts"] == dict(requested=4, eligible=2, already_done=1, refused=1)
    assert receipt["totals"]["eligible"] == dict(subtotal="400.00", tax="30.00", total="430.00")
    assert receipt["refusals_by_reason"] == {"not_approved": 1}
    assert receipt["by_period"] == {"2026-01-01..2026-01-31": dict(count=2, total="430.00")}
    assert receipt["posted_total"] == "430.00" and receipt["applied"] == 2
    text = json.dumps(receipt)
    assert "invoice_id" not in text and not any(str(UUID(int=n)) in text for n in range(10, 14))


def test_plan_receipt_has_no_apply_fields():
    receipt = cli.receipt_of(plan(), mode="plan")
    assert receipt["mode"] == "plan" and "applied" not in receipt and receipt["production_ready"] is False


def test_apply_without_authorization_refuses_before_any_file_or_database_use(monkeypatch, tmp_path, capsys):
    for name in ("ERP_IMPORT_APPROVED", "ERP_IMPORT_APPLY_OK"):
        monkeypatch.delenv(name, raising=False)
    receipt = tmp_path / "receipt.json"
    code = cli.main(
        [
            "--manifest", str(tmp_path / "m.json"),
            "--snapshot-file", str(tmp_path / "s"),
            "--list", str(tmp_path / "l.json"),
            "--receipt", str(receipt),
            "--plan-file", str(tmp_path / "plan.json"),
            "--list-sha256", "c" * 64,
            "--approver-party-id", str(UUID(int=5)),
            "--apply",
        ]
    )  # fmt: skip
    assert code == 2 and not receipt.exists() and not (tmp_path / "plan.json").exists()
    assert "refused" in capsys.readouterr().out


def test_plan_mode_failure_receipt_is_private_and_opaque(monkeypatch, tmp_path):
    for name in ("LEGACY_DATABASE_URL", "MIGRATION_DATABASE_URL"):
        monkeypatch.setenv(name, "postgresql://secret-user:secret-pw@127.0.0.1/none")
    receipt = tmp_path / "receipt.json"
    code = cli.main(
        [
            "--manifest", str(tmp_path / "missing.json"),
            "--snapshot-file", str(tmp_path / "s"),
            "--list", str(tmp_path / "l.json"),
            "--receipt", str(receipt),
            "--plan-file", str(tmp_path / "plan.json"),
            "--list-sha256", "c" * 64,
            "--approver-party-id", str(UUID(int=5)),
        ]
    )  # fmt: skip
    assert code == 2
    assert oct(os.stat(receipt).st_mode & 0o777) == "0o600"
    payload = json.loads(receipt.read_text())
    assert payload == dict(production_ready=False, refusal="recognize_command_refused")
    assert "secret" not in receipt.read_text()
    # DSNs were removed from the environment before use.
    assert "LEGACY_DATABASE_URL" not in os.environ and "MIGRATION_DATABASE_URL" not in os.environ


def test_receipt_path_symlink_is_refused(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("keep")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        cli._receipt(link)
    assert target.read_text() == "keep"


def test_preflight_is_the_historical_destination_preflight_and_keeps_its_refusals(monkeypatch):
    from erp_v2.importer import payables_history_cli as history

    seen = []
    monkeypatch.setattr(history, "_destination_preflight", lambda session, tenant: seen.append((session, tenant)))
    cli._preflight("session", UUID(TENANT))
    assert seen == [("session", UUID(TENANT))]

    def refuse(session, tenant):
        raise history.OperatorRefusal("destination_role_not_nonprivileged")

    monkeypatch.setattr(history, "_destination_preflight", refuse)
    with pytest.raises(cli.RecognizeRefusal, match="destination_role_not_nonprivileged"):
        cli._preflight("session", UUID(TENANT))


def test_plan_file_path_must_be_new_private_and_not_a_symlink(monkeypatch, tmp_path, capsys):
    taken = tmp_path / "plan.json"
    taken.write_text("keep")
    args = [
        "--manifest", str(tmp_path / "m.json"),
        "--snapshot-file", str(tmp_path / "s"),
        "--list", str(tmp_path / "l.json"),
        "--receipt", str(tmp_path / "receipt.json"),
        "--plan-file", str(taken),
        "--list-sha256", "c" * 64,
        "--approver-party-id", str(UUID(int=5)),
    ]  # fmt: skip
    assert cli.main(args) == 2 and taken.read_text() == "keep"
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "elsewhere.json")
    args[args.index("--plan-file") + 1] = str(link)
    args[args.index("--receipt") + 1] = str(tmp_path / "receipt2.json")
    assert cli.main(args) == 2 and not (tmp_path / "elsewhere.json").exists()


def test_failed_run_leaves_a_private_empty_plan_file(monkeypatch, tmp_path):
    for name in ("LEGACY_DATABASE_URL", "MIGRATION_DATABASE_URL"):
        monkeypatch.setenv(name, "postgresql://u:p@127.0.0.1/none")
    plan_file = tmp_path / "plan.json"
    code = cli.main(
        [
            "--manifest", str(tmp_path / "missing.json"),
            "--snapshot-file", str(tmp_path / "s"),
            "--list", str(tmp_path / "l.json"),
            "--receipt", str(tmp_path / "receipt.json"),
            "--plan-file", str(plan_file),
            "--list-sha256", "c" * 64,
            "--approver-party-id", str(UUID(int=5)),
        ]
    )  # fmt: skip
    assert code == 2 and oct(os.stat(plan_file).st_mode & 0o777) == "0o600" and plan_file.read_text() == ""


def test_plan_file_carries_ids_dates_amounts_and_reasons_only():
    detail = [
        dict(invoice_id=str(UUID(int=10)), outcome="eligible", reason=None, invoice_date="2026-01-10",
             subtotal="200.00", tax="15.00", total="215.00"),
        dict(invoice_id=str(UUID(int=13)), outcome="refused", reason="not_approved", invoice_date="2026-02-10",
             subtotal="1.00", tax="0.00", total="1.00"),
    ]  # fmt: skip
    payload = cli.plan_file_of(plan() | {"detail": detail})
    assert payload["plan_digest"] == "f" * 64 and payload["invoices"] == detail
    assert set(payload) == {"version", "plan_digest", "bindings", "invoices"}
    assert "invoices" not in cli.receipt_of(plan() | {"detail": detail}, mode="plan")
