"""Settled historical admission operator: receipt shape, plan logic and authorization gates (no database)."""

import json
import os
from types import SimpleNamespace
from uuid import UUID

import pytest

from erp_v2.importer import payables_history_cli as history
from erp_v2.legacy_ap import recognize_cli
from erp_v2.legacy_ap import settled_cli as cli

TENANT = str(UUID(int=1))


def facts(**change):
    return dict(total="200.00", settled="80.00", withholding="8.00", residual="120.00", settlements=1) | change


def plan():
    return dict(
        version=1,
        bindings=dict(list_sha256="c" * 64),
        eligible=[
            dict(invoice_id=str(UUID(int=n)), evidence_fingerprint="cv1:" + "e" * 64, **facts()) for n in (10, 11)
        ],
        already_done=[dict(invoice_id=str(UUID(int=12)), **facts())],
        refused=[dict(invoice_id=str(UUID(int=13)), reason="legacy_evidence:payment_not_posted")],
        digest="f" * 64,
    )


def test_list_format_and_bindings_are_the_b1b_ones():
    assert cli.parse_list is recognize_cli.parse_list and cli.SettledRefusal is recognize_cli.RecognizeRefusal


def test_receipt_holds_counts_and_money_totals_only():
    receipt = cli.receipt_of(plan(), mode="apply", applied=2)
    assert receipt["counts"] == dict(requested=4, eligible=2, already_done=1, refused=1)
    assert receipt["totals"]["eligible"] == dict(
        total="400.00", settled="160.00", withholding="16.00", residual="240.00", settlements=2
    )
    assert receipt["totals"]["already_done"]["settled"] == "80.00"
    assert receipt["refusals_by_reason"] == {"legacy_evidence:payment_not_posted": 1} and receipt["applied"] == 2
    text = json.dumps(receipt)
    assert "invoice_id" not in text and not any(str(UUID(int=n)) in text for n in range(10, 14))
    assert "evidence_fingerprint" not in text


def test_plan_receipt_has_no_apply_fields():
    receipt = cli.receipt_of(plan(), mode="plan")
    assert receipt["mode"] == "plan" and "applied" not in receipt and receipt["production_ready"] is False


def test_private_plan_file_holds_the_detail_and_the_receipt_does_not():
    detail = [dict(invoice_id=str(UUID(int=10)), outcome="eligible", reason=None, **facts())]
    payload = cli.plan_file_of(plan() | {"detail": detail})
    assert payload["invoices"] == detail and set(payload) == {"version", "plan_digest", "bindings", "invoices"}
    assert "invoices" not in cli.receipt_of(plan() | {"detail": detail}, mode="plan")


class FakeVerifier:
    """Stands in for SettledInvoiceVerifier: evidence per invoice, no database."""

    source = None

    def __init__(self, settlements):
        self.settlements = settlements

    @staticmethod
    def review(**_):
        return None  # no Finance decision on file

    @staticmethod
    def command(evidence):
        return SimpleNamespace(evidence_fingerprint="cv1:" + "a" * 64)

    def evidence(self, *, scope, invoice_id, destination):
        return SimpleNamespace(
            settlements=[SimpleNamespace(source_settlement_id=s) for s in self.settlements[invoice_id]],
            total_amount=200,
            settled_amount=80,
            withholding_tax_amount=0,
            residual_amount=120,
        )


def test_a_legacy_allocation_presented_for_two_invoices_refuses_both(monkeypatch):
    first, second, third = (UUID(int=n) for n in (1, 2, 3))
    verifier = FakeVerifier({first: ["alloc-1"], second: ["alloc-1"], third: ["alloc-3"]})
    monkeypatch.setattr(
        cli,
        "_classify",
        lambda session, scope, v, invoice_id, referenced: (
            None,
            v.evidence(scope=scope, invoice_id=invoice_id, destination=None),
        ),
    )
    result = cli.build_plan(
        None, SimpleNamespace(tenant_id=UUID(TENANT)), verifier, (first, second, third), bindings={}
    )
    assert [r["invoice_id"] for r in result["eligible"]] == [str(third)]
    assert sorted((r["invoice_id"], r["reason"]) for r in result["refused"]) == sorted(
        [(str(first), "settlement_id_duplicate"), (str(second), "settlement_id_duplicate")]
    )
    # The digest is a function of the plan content only.
    again = cli.build_plan(None, SimpleNamespace(tenant_id=UUID(TENANT)), verifier, (first, second, third), bindings={})
    assert again["digest"] == result["digest"]


def test_identity_key_binds_invoice_and_snapshot():
    a = cli._identity_key(UUID(int=1), "d" * 64)
    assert a == cli._identity_key(UUID(int=1), "d" * 64)
    assert a != cli._identity_key(UUID(int=2), "d" * 64) and a != cli._identity_key(UUID(int=1), "e" * 64)
    assert a.startswith("erp.settled-admission:") and len(a) < 255


def test_preflight_uses_the_inherited_preflight_with_only_historical_settlements_exempt(monkeypatch):
    seen = []
    monkeypatch.setattr(history, "_destination_preflight", lambda session, tenant, tables: seen.append(tables))
    cli._preflight("session", UUID(TENANT))
    assert seen == [history.SETTLED_PREIMPORT_EMPTY_TABLES]
    assert set(history.PREIMPORT_EMPTY_TABLES) - set(seen[0]) == {"historical_settlements"}

    def refuse(session, tenant, tables):
        raise history.OperatorRefusal("destination_role_not_nonprivileged")

    monkeypatch.setattr(history, "_destination_preflight", refuse)
    with pytest.raises(cli.SettledRefusal, match="destination_role_not_nonprivileged"):
        cli._preflight("session", UUID(TENANT))


def test_the_unpaid_import_keeps_the_full_empty_table_rule():
    import inspect

    default = inspect.signature(history._destination_preflight).parameters["empty_tables"].default
    assert default == history.PREIMPORT_EMPTY_TABLES and "historical_settlements" in default


def args_for(tmp_path, *extra):
    return [
        "--manifest", str(tmp_path / "m.json"),
        "--snapshot-file", str(tmp_path / "s"),
        "--list", str(tmp_path / "l.json"),
        "--receipt", str(tmp_path / "receipt.json"),
        "--plan-file", str(tmp_path / "plan.json"),
        "--list-sha256", "c" * 64,
        "--approver-party-id", str(UUID(int=5)),
        *extra,
    ]  # fmt: skip


def test_apply_without_authorization_refuses_before_any_file_or_database_use(monkeypatch, tmp_path, capsys):
    for name in ("ERP_IMPORT_APPROVED", "ERP_IMPORT_APPLY_OK"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(args_for(tmp_path, "--apply")) == 2
    assert not (tmp_path / "receipt.json").exists() and not (tmp_path / "plan.json").exists()
    assert "refused" in capsys.readouterr().out


def test_failure_receipt_and_plan_file_are_private_and_opaque(monkeypatch, tmp_path):
    for name in ("LEGACY_DATABASE_URL", "MIGRATION_DATABASE_URL"):
        monkeypatch.setenv(name, "postgresql://secret-user:secret-pw@127.0.0.1/none")
    assert cli.main(args_for(tmp_path)) == 2
    for name in ("receipt.json", "plan.json"):
        assert oct(os.stat(tmp_path / name).st_mode & 0o777) == "0o600"
    payload = json.loads((tmp_path / "receipt.json").read_text())
    assert payload == dict(production_ready=False, refusal="settled_admission_command_refused")
    assert "secret" not in (tmp_path / "receipt.json").read_text() and (tmp_path / "plan.json").read_text() == ""
    assert "LEGACY_DATABASE_URL" not in os.environ and "MIGRATION_DATABASE_URL" not in os.environ


def test_existing_plan_file_is_never_overwritten(tmp_path):
    taken = tmp_path / "plan.json"
    taken.write_text("keep")
    assert cli.main(args_for(tmp_path)) == 2 and taken.read_text() == "keep"


# --- prerequisite receipts -----------------------------------------------------------------------------------

SNAP, REV = "b" * 64, "a" * 40
MANIFEST = SimpleNamespace(snapshot_sha256=SNAP, candidate_revision=REV)


def receipt_file(tmp_path, name, **change):
    kind = name.startswith("unpaid")
    data = dict(
        mode="apply",
        counts=dict(requested=1, eligible=1, refused=0),
        bindings=dict(tenant_id=TENANT, snapshot_sha256=SNAP, candidate_revision=REV),
        **({"admitted": 1} if kind else {"applied": 1, "posted_total": "1.00"}),
    )
    data.update(change)
    path = tmp_path / name
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    import hashlib

    return path, hashlib.sha256(raw).hexdigest()


def prereq_args(tmp_path, apply=True, **change):
    (u, us), (b, bs) = receipt_file(tmp_path, "unpaid.json"), receipt_file(tmp_path, "b1b.json")
    base = dict(apply=apply, unpaid_receipt=u, unpaid_receipt_sha256=us, b1b_receipt=b, b1b_receipt_sha256=bs)
    return SimpleNamespace(**(base | change))


def test_both_receipts_verify_and_their_digests_are_returned(tmp_path):
    args = prereq_args(tmp_path)
    got = cli.verify_prerequisites(args, UUID(TENANT), MANIFEST)
    assert got == dict(unpaid_receipt_sha256=args.unpaid_receipt_sha256, b1b_receipt_sha256=args.b1b_receipt_sha256)


def test_plan_mode_may_omit_them_but_apply_may_not(tmp_path):
    none = dict(unpaid_receipt=None, unpaid_receipt_sha256=None, b1b_receipt=None, b1b_receipt_sha256=None)
    assert cli.verify_prerequisites(prereq_args(tmp_path, apply=False, **none), UUID(TENANT), MANIFEST) == {
        "unpaid_receipt_sha256": None,
        "b1b_receipt_sha256": None,
    }
    for omitted in (none, {"b1b_receipt": None, "b1b_receipt_sha256": None}, {"unpaid_receipt_sha256": None}):
        with pytest.raises(cli.SettledRefusal, match="prerequisite_receipt_missing"):
            cli.verify_prerequisites(prereq_args(tmp_path, **omitted), UUID(TENANT), MANIFEST)
    with pytest.raises(cli.SettledRefusal, match="prerequisite_receipt_missing"):
        cli.verify_prerequisites(prereq_args(tmp_path, b1b_receipt=tmp_path / "absent.json"), UUID(TENANT), MANIFEST)


@pytest.mark.parametrize(
    "name,change",
    [
        ("unpaid.json", dict(mode="plan")),
        ("unpaid.json", dict(counts=dict(refused=1))),
        ("unpaid.json", dict(bindings=dict(tenant_id=str(UUID(int=9)), snapshot_sha256=SNAP))),
        ("unpaid.json", dict(bindings=dict(tenant_id=TENANT, snapshot_sha256="c" * 64))),
        ("unpaid.json", dict(posted_total="1.00")),  # a B1b-shaped receipt in the unpaid slot
        ("b1b.json", dict(admitted=1)),  # an unpaid-shaped receipt in the B1b slot
        ("b1b.json", dict(mode="plan")),
        ("b1b.json", dict(bindings=dict(tenant_id=TENANT, snapshot_sha256=SNAP, candidate_revision="f" * 40))),
    ],
)
def test_a_receipt_of_the_wrong_kind_tenant_snapshot_or_state_is_refused(tmp_path, name, change):
    path, sha = receipt_file(tmp_path, name, **change)
    key = "unpaid" if name.startswith("unpaid") else "b1b"
    args = prereq_args(tmp_path, **{key + "_receipt": path, key + "_receipt_sha256": sha})
    with pytest.raises(cli.SettledRefusal, match="prerequisite_receipt_mismatch"):
        cli.verify_prerequisites(args, UUID(TENANT), MANIFEST)


def test_a_receipt_whose_bytes_do_not_match_the_given_digest_is_refused(tmp_path):
    args = prereq_args(tmp_path, b1b_receipt_sha256="0" * 64)
    with pytest.raises(cli.SettledRefusal, match="prerequisite_receipt_mismatch"):
        cli.verify_prerequisites(args, UUID(TENANT), MANIFEST)
