"""Operator tool: build, sign and clear the rounding residue of the reconciliation bridge (B-9).

    python -m erp_v2.migration_ledger.bridge_report build --spec S.json --inputs I.json --itemised-out F \
        --receipt R.json --operator-ref <who> [--apply --approved-sha256 <body sha from the dry run>]
    python -m erp_v2.migration_ledger.bridge_report sign --spec S.json --version N --expect-sha <sha> \
        --signed-by <name> --receipt R.json --operator-ref <who> --apply
    python -m erp_v2.migration_ledger.bridge_report rounding --spec S.json --receipt R.json --operator-ref <who> \
        [--apply]

``S.json`` is ``{"version": 1, "tenant_id": "<uuid>", "requester_party_id": "<uuid>", "approver_party_id": "<uuid>",
"evidence_ref": "<text>"}`` (the parties are used by ``rounding``). Dry run is the default; every write needs
``--apply`` AND ``ERP_MIGRATION_LEDGER_APPLY=1``. The itemised file is written 0600, exclusively. See ``bridge``
for the body, the partition proof and the sign refusals. ``rounding`` posts ONE correction per period through the
governed B2 path (prepare, request, approve) against the Finance-named rounding account declared in the bridge.
"""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.migration_ledger import bridge, rounding_verify
from erp_v2.migration_ledger import cli_common as cli
from erp_v2.migration_ledger.common import keys_exactly, refuse, sha_text, strict_json, text, uuid_text

_SPEC_KEYS = {"version", "tenant_id", "requester_party_id", "approver_party_id", "evidence_ref"}


def _spec(path: Path) -> tuple[Any, dict[str, Any]]:
    data, sha = cli.read_bound(path)
    spec = keys_exactly(strict_json(data, "invalid_spec_json"), _SPEC_KEYS, "invalid_spec_schema")
    if spec["version"] != 1:
        refuse("invalid_spec_schema")
    return uuid_text(spec["tenant_id"], "invalid_tenant_id"), {
        "spec_sha256": sha,
        "requester": uuid_text(spec["requester_party_id"], "invalid_requester_party_id"),
        "approver": uuid_text(spec["approver_party_id"], "invalid_approver_party_id"),
        "evidence_ref": text(spec["evidence_ref"], "evidence_ref_required", 255),
    }


def _write_private(path: Path, body: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(body, stream, indent=1, sort_keys=True)
        stream.write("\n")


def _summary(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "body_sha256": bridge.body_sha256(body),
        "ready": body["ready"],
        "rows": len(body["rows"]),
        "partition": body["partition"],
        "partition_gap_abs_total": body["partition_gap_abs_total"],
        "queue_abs_total": body["queue_abs_total"],
        "rounding_residue_periods": len(body["rounding_residue_by_period"]),
        "itemised_sha256": body["itemised_sha256"],
    }


def _run(args) -> dict[str, Any]:
    operator = cli.operator(args.operator_ref)
    tenant, spec = _spec(args.spec)
    eng = cli.engine()  # the DSN leaves the environment BEFORE the assembly is imported
    cli.install_registries()
    try:
        with Session(eng) as db:
            if not db.scalar(sa.text("SELECT is_active FROM tenants WHERE id = :t"), {"t": tenant}):
                refuse("tenant_missing_or_inactive")
            if args.mode == "build":
                data, inputs_sha = cli.read_bound(args.inputs)
                inputs = bridge.parse_inputs(strict_json(data, "invalid_bridge_inputs_json"), inputs_sha)
                opening_net, ias8, tb_sha, ias8_sha = bridge.ledger_inputs(db, tenant)
                body, itemised = bridge.compute(
                    inputs, opening_net=opening_net, ias8=ias8, opening_tb_sha=tb_sha, ias8_sha=ias8_sha
                )
                out = {"mode": "build", "applied": False, **_summary(body)}
                if not args.apply:
                    db.rollback()
                    return out
                cli.require_apply(args.apply)
                if not args.approved_sha256 or args.approved_sha256 != out["body_sha256"]:
                    refuse("bridge_sha_not_approved")
                if not args.itemised_out:
                    refuse("itemised_out_required")
                row, created = bridge.store_version(db, tenant, body, created_by=operator)
                if created:
                    _write_private(args.itemised_out, itemised)
                db.commit()
                return {**out, "applied": True, "version": row.version, "created": created}
            if args.mode == "sign":
                cli.require_apply(args.apply)
                row, signed_now = bridge.sign_version(
                    db,
                    tenant,
                    version=args.version,
                    expect_sha=sha_text(args.expect_sha, "invalid_expect_sha"),
                    signed_by=text(args.signed_by, "signed_by_required"),
                )
                db.commit()
                return {
                    "mode": "sign",
                    "version": row.version,
                    "sha256": row.sha256,
                    "signed_by": row.signed_by,
                    "signed_now": signed_now,
                }
            # rounding
            latest = bridge.latest(db, tenant)
            if latest is None:
                refuse("no_bridge_version")
            body = bridge.verified_body(latest)
            due = {p: str(rounding_verify.due(db, tenant, body, p)) for p in sorted(body["rounding_residue_by_period"])}
            out = {
                "mode": "rounding",
                "applied": False,
                "bridge_sha256": latest.sha256,
                "periods": len(due),
                "due_by_period": due,
            }
            if not args.apply:
                db.rollback()
                return out
            cli.require_apply(args.apply)
            from erp_v2.journals import service as journals

            posted = 0
            for period, amount in due.items():
                if Decimal(amount) == 0:
                    continue
                prepared = journals.prepare_rounding_correction(
                    db,
                    tenant_id=tenant,
                    actor=spec["requester"],
                    command=journals.RoundingCorrection(
                        latest.sha256,
                        period,
                        body.get("rounding_account_code"),
                        spec["evidence_ref"],
                        f"Rounding residue {period} (T-ROUND-2DP), bridge v{latest.version}",
                    ),
                )
                requested = journals.request_post(
                    db, tenant_id=tenant, manual_id=prepared.manual_id, actor=spec["requester"]
                )
                journals.approve_and_post(
                    db, tenant_id=tenant, manual_id=requested.manual_id, approver=spec["approver"]
                )
                posted += 1
            db.commit()
            return {
                **out,
                "applied": True,
                "posted": posted,
                "pending_after": rounding_verify.pending_periods(db, tenant, body),
            }
    finally:
        eng.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.migration_ledger.bridge_report")
    parser.add_argument("mode", choices=("build", "sign", "rounding"))
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--operator-ref", required=True)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--itemised-out", type=Path)
    parser.add_argument("--approved-sha256")
    parser.add_argument("--version", type=int)
    parser.add_argument("--expect-sha")
    parser.add_argument("--signed-by")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "build" and not args.inputs:
        parser.error("build needs --inputs")
    if args.mode == "sign" and (args.version is None or not args.expect_sha or not args.signed_by):
        parser.error("sign needs --version, --expect-sha and --signed-by")
    return cli.run(
        args.receipt,
        lambda: _run(args),
        "bridge report",
        pre=(lambda: cli.require_apply(args.apply)) if args.mode == "sign" else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
