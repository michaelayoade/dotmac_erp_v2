"""Operator plan/apply: the reviewed FY2025 opening journal and the IAS 8 journals (B-9, Tier C).

    {"version": 1, "tenant_id": "<uuid>", "tb_sha256": "<sha of the reviewed TB file>",
     "subledger_sha256": "<sha of the subledger openings file>", "ias8_sha256": "<sha of the IAS 8 file>",
     "evidence_ref": "<who/why, <=255>", "requester_party_id": "<uuid>", "approver_party_id": "<uuid>"}

    python -m erp_v2.migration_ledger.opening_cli plan --spec S.json --tb TB.json --subledger SL.json --ias8 IA.json \
        --receipt P.json --operator-ref <who>
    ERP_MIGRATION_LEDGER_APPLY=1 python -m erp_v2.migration_ledger.opening_cli apply ... --plan-receipt P.json \
        --approved-plan-sha256 <plan_sha256 in P.json> --receipt A.json --operator-ref <who> --apply

REFUSES unless the trial balance sums to zero and every control account (2000, 2040, 1410, 1400) equals its
subledger opening once the itemised IAS 8 movements are applied (see ``opening``). Plan writes nothing; apply
recomputes the plan and refuses on any drift. DSN: ``MIGRATION_DATABASE_URL`` only. Receipts are 0600, exclusive,
and carry shas, counts and control-account tie-outs only. There is no undo: the only rollback is not to apply, and
the migration downgrade refuses once provenance exists.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.migration_ledger import cli_common as cli
from erp_v2.migration_ledger import opening
from erp_v2.migration_ledger.common import keys_exactly, refuse, sha_text, strict_json, text, uuid_text

_SPEC_KEYS = {
    "version",
    "tenant_id",
    "tb_sha256",
    "subledger_sha256",
    "ias8_sha256",
    "evidence_ref",
    "requester_party_id",
    "approver_party_id",
}


def load_inputs(args) -> tuple[Any, opening.OpeningInputs, str, dict[str, Any]]:
    spec_bytes, spec_sha = cli.read_bound(args.spec)
    spec = keys_exactly(strict_json(spec_bytes, "invalid_spec_json"), _SPEC_KEYS, "invalid_spec_schema")
    if spec["version"] != 1:
        refuse("invalid_spec_schema")
    tenant = uuid_text(spec["tenant_id"], "invalid_tenant_id")
    tb_bytes, tb_sha = cli.read_bound(args.tb, sha_text(spec["tb_sha256"], "invalid_spec_schema"))
    sl_bytes, sl_sha = cli.read_bound(args.subledger, sha_text(spec["subledger_sha256"], "invalid_spec_schema"))
    ia_bytes, ia_sha = cli.read_bound(args.ias8, sha_text(spec["ias8_sha256"], "invalid_spec_schema"))
    as_of, tb = opening.parse_tb(strict_json(tb_bytes, "invalid_tb_json"), tb_sha)
    inputs = opening.OpeningInputs(
        as_of,
        tb,
        tb_sha,
        opening.parse_subledger(strict_json(sl_bytes, "invalid_subledger_json")),
        sl_sha,
        opening.parse_ias8(strict_json(ia_bytes, "invalid_ias8_json")),
        ia_sha,
    )
    parties = {
        "requester": uuid_text(spec["requester_party_id"], "invalid_requester_party_id"),
        "approver": uuid_text(spec["approver_party_id"], "invalid_approver_party_id"),
        "evidence_ref": text(spec["evidence_ref"], "evidence_ref_required", 255),
    }
    return tenant, inputs, spec_sha, parties


def _run(args) -> dict[str, Any]:
    applying = args.mode == "apply"
    operator = cli.operator(args.operator_ref)
    tenant, inputs, spec_sha, parties = load_inputs(args)
    proof = opening.opening_proof(inputs)
    plan = opening.plan_sha256(inputs, proof)
    planned_by = operator
    receipt_sha = ""
    if applying:
        if not args.plan_receipt or not args.approved_plan_sha256:
            refuse("plan_binding_required")
        data, receipt_sha = cli.read_bound(args.plan_receipt)
        prior = strict_json(data, "invalid_plan_receipt")
        if (
            not isinstance(prior, dict)
            or prior.get("mode") != "plan"
            or prior.get("plan_sha256") != args.approved_plan_sha256
            or prior.get("spec_sha256") != spec_sha
            or not prior.get("ready")
        ):
            refuse("plan_binding_mismatch")
        if plan != args.approved_plan_sha256:
            refuse("plan_drift")
        planned_by = cli.operator(prior.get("prepared_by"))
    out: dict[str, Any] = {
        "mode": args.mode,
        "spec_sha256": spec_sha,
        "plan_sha256": plan,
        "proof": proof,
        "ias8_pending_dates": None,
        ("applied_by" if applying else "prepared_by"): operator,
    }
    out["ready"] = bool(proof["ok"])
    if not proof["ok"]:
        out["refusal"] = "opening_tb_not_zero_sum" if not proof["zero_sum"] else "opening_control_not_subledger"
        return out  # refuse before any database access
    eng = cli.engine()  # the DSN leaves the environment BEFORE the assembly is imported
    cli.install_registries()
    from erp_v2.governance import policy as governance

    try:
        with Session(eng) as db:
            if not applying:
                db.execute(sa.text("SET TRANSACTION READ ONLY"))
            if not db.scalar(sa.text("SELECT is_active FROM tenants WHERE id = :t"), {"t": tenant}):
                refuse("tenant_missing_or_inactive")
            dual = governance.dual_control(db, tenant)
            out["dual_control"] = dual
            out["state"] = opening.check_state(db, tenant, inputs)
            if not applying:
                db.rollback()
                return out
            cli.require_apply(args.apply)
            if dual and planned_by == operator:
                refuse("dual_control_same_operator")
            result = opening.apply_opening(
                db,
                tenant,
                inputs,
                opening.ApplyInputs(
                    spec_sha,
                    plan,
                    planned_by,
                    operator,
                    parties["requester"],
                    parties["approver"],
                    dual,
                    parties["evidence_ref"],
                ),
            )
            db.commit()
            out["applied"] = result
            out["plan_receipt_sha256"] = receipt_sha
            out["state"] = opening.check_state(db, tenant, inputs)
            return out
    finally:
        eng.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.migration_ledger.opening_cli")
    parser.add_argument("mode", choices=("plan", "apply"))
    for name in ("spec", "tb", "subledger", "ias8", "receipt"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--plan-receipt", type=Path)
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument("--operator-ref", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    return cli.run(
        args.receipt,
        lambda: _run(args),
        "opening",
        pre=(lambda: cli.require_apply(args.apply)) if args.mode == "apply" else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
