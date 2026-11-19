"""Operator plan/apply: the reviewed FY2025 opening journal and the IAS 8 journals (B-9, Tier C).

    {"version": 1, "tenant_id": "<uuid>", "tb_sha256": "<sha of the reviewed TB file>",
     "ias8_sha256": "<sha of the IAS 8 file>",
     "evidence_ref": "<who/why, <=255>", "requester_party_id": "<uuid>", "approver_party_id": "<uuid>"}

    python -m erp_v2.migration_ledger.opening_cli plan --spec S.json --tb TB.json --ias8 IA.json \
        --receipt P.json --operator-ref <who>
    ERP_MIGRATION_LEDGER_APPLY=1 python -m erp_v2.migration_ledger.opening_cli apply ... --plan-receipt P.json \
        --approved-plan-sha256 <plan_sha256 in P.json> --receipt A.json --operator-ref <who> --apply

REFUSES unless the trial balance sums to zero and every control account (2000, 2040, 1410, 1400) equals its
subledger opening once the itemised IAS 8 movements are applied (see ``opening``). Plan writes nothing; apply
recomputes the plan and refuses on any drift. DSNs, environment only: ``MIGRATION_DATABASE_URL`` (destination) and
``LEGACY_DATABASE_URL`` (read-only legacy snapshot; the subledger opening is RECOMPUTED from it, never declared).
Receipts are 0600, exclusive,
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
from erp_v2.migration_ledger import legacy_subledger, opening
from erp_v2.migration_ledger.common import (
    ZERO,
    canonical_sha,
    fmt,
    keys_exactly,
    refuse,
    sha_text,
    strict_json,
    text,
    uuid_text,
)

_SPEC_KEYS = {
    "version",
    "tenant_id",
    "tb_sha256",
    "ias8_sha256",
    "evidence_ref",
    "requester_party_id",
    "approver_party_id",
}


def load_inputs(args) -> dict[str, Any]:
    spec_bytes, spec_sha = cli.read_bound(args.spec)
    spec = keys_exactly(strict_json(spec_bytes, "invalid_spec_json"), _SPEC_KEYS, "invalid_spec_schema")
    if spec["version"] != 1:
        refuse("invalid_spec_schema")
    tb_bytes, tb_sha = cli.read_bound(args.tb, sha_text(spec["tb_sha256"], "invalid_spec_schema"))
    ia_bytes, ia_sha = cli.read_bound(args.ias8, sha_text(spec["ias8_sha256"], "invalid_spec_schema"))
    as_of, tb = opening.parse_tb(strict_json(tb_bytes, "invalid_tb_json"), tb_sha)
    return {
        "tenant": uuid_text(spec["tenant_id"], "invalid_tenant_id"),
        "as_of": as_of,
        "tb": tb,
        "tb_sha": tb_sha,
        "ias8": opening.parse_ias8(strict_json(ia_bytes, "invalid_ias8_json")),
        "ias8_sha": ia_sha,
        "spec_sha": spec_sha,
        "requester": uuid_text(spec["requester_party_id"], "invalid_requester_party_id"),
        "approver": uuid_text(spec["approver_party_id"], "invalid_approver_party_id"),
        "evidence_ref": text(spec["evidence_ref"], "evidence_ref_required", 255),
    }


def _recompute_subledger(eng, legacy_eng, tenant, as_of) -> dict[str, Any]:
    """Sub(2000, 1400, 2040, 1410) at the opening date from the legacy snapshot (read only)."""
    from dotmac_accounting.models import Account

    from erp_v2.journals.policy import functional_currency

    with Session(eng) as db:
        functional = functional_currency(db, tenant)
        family = [
            str(i)
            for i in db.scalars(
                sa.select(Account.id).where(
                    Account.tenant_id == tenant, Account.code.in_(legacy_subledger.CLAIM_FAMILY)
                )
            )
        ]
    with legacy_eng.connect() as legacy, legacy.begin():
        legacy.execute(sa.text("SET TRANSACTION READ ONLY"))
        legacy.execute(sa.text("SET LOCAL row_security = off"))
        return legacy_subledger.recompute(legacy, as_of, family, functional)


def _run(args) -> dict[str, Any]:
    applying = args.mode == "apply"
    operator = cli.operator(args.operator_ref)
    given = load_inputs(args)
    tenant, spec_sha = given["tenant"], given["spec_sha"]
    out: dict[str, Any] = {
        "mode": args.mode,
        "spec_sha256": spec_sha,
        ("applied_by" if applying else "prepared_by"): operator,
        "ready": False,
    }
    if sum(given["tb"].values(), ZERO) != 0:
        out["refusal"] = "opening_tb_not_zero_sum"  # refused before any database access
        return out
    eng = cli.engine()  # the DSNs leave the environment BEFORE the assembly is imported
    legacy_eng = cli.legacy_engine()
    cli.install_registries()
    from erp_v2.governance import policy as governance

    try:
        sub = _recompute_subledger(eng, legacy_eng, tenant, given["as_of"])
        inputs = opening.OpeningInputs(
            given["as_of"], given["tb"], given["tb_sha"], sub,
            canonical_sha({k: fmt(v) for k, v in sorted(sub.items())}), given["ias8"], given["ias8_sha"],
        )  # fmt: skip
        proof = opening.opening_proof(inputs)
        plan = opening.plan_sha256(inputs, proof)
        out.update(plan_sha256=plan, proof=proof, ready=bool(proof["ok"]))
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
        if not proof["ok"]:
            out["refusal"] = "opening_control_not_subledger"
            return out  # every GL - subledger difference must be covered EXACTLY by itemised IAS 8 items
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
                    spec_sha, plan, planned_by, operator, given["requester"], given["approver"], dual,
                    given["evidence_ref"],
                ),
            )  # fmt: skip
            db.commit()
            out["applied"] = result
            out["plan_receipt_sha256"] = receipt_sha
            out["state"] = opening.check_state(db, tenant, inputs)
            return out
    finally:
        eng.dispose()
        legacy_eng.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.migration_ledger.opening_cli")
    parser.add_argument("mode", choices=("plan", "apply"))
    for name in ("spec", "tb", "ias8", "receipt"):
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
