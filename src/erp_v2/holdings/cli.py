"""Operator CLI for the migration holdings (B14.5 - B14.7). Dry-run by default; env-only DSN; 0600 receipts.

    python -m erp_v2.holdings.cli t1 plan     --spec S.json --receipt P.json --operator-ref <who>
    ERP_HOLDING_APPLY=1 python -m erp_v2.holdings.cli t1 apply --spec S.json --plan-receipt P.json --plan-sha <sha> ...
    python -m erp_v2.holdings.cli plan        --spec S.json --receipt P.json --operator-ref <who>
    ERP_HOLDING_APPLY=1 python -m erp_v2.holdings.cli apply --spec S.json --plan-receipt P.json --plan-sha <sha> ...
    python -m erp_v2.holdings.cli verify      --spec S.json --receipt V.json
    python -m erp_v2.holdings.cli release     --spec S.json --exception <key> --owner-command C.json ...   (post-flip)
    python -m erp_v2.holdings.cli writeoff plan|apply --spec S.json --exception <key> --offset-account <code> ...

DSN: ``MIGRATION_DATABASE_URL`` only (popped); serving DSNs are refused. The write commands also need
``ERP_HOLDING_APPLY=1``. Receipts are created exclusively before any read or write and carry the plan or the outcome.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from erp_v2.holdings import apply as applying
from erp_v2.holdings import attribution as attr
from erp_v2.holdings import plan as planning
from erp_v2.holdings import release as releasing
from erp_v2.holdings import t1 as t1_mod
from erp_v2.holdings.billing import BillingReader, default_reader
from erp_v2.holdings.errors import refuse
from erp_v2.migration_ledger import cli_common
from erp_v2.migration_ledger.common import sha_text, strict_json, text

APPLY_ENV = "ERP_HOLDING_APPLY"


def billing_reader() -> BillingReader:  # patched by tests and by the a13 binding
    return default_reader()


def _require_apply(flag: bool) -> None:
    if not flag or os.environ.get(APPLY_ENV) != "1":
        refuse("explicit_holding_apply_required")


def _read_bound(ref: planning.ReceiptRef) -> tuple[bytes, str]:
    raw = Path(ref.path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != ref.sha256:
        refuse("input_sha_mismatch", Path(ref.path).name)
    return raw, sha


def load_receipts(spec: planning.RunSpec) -> planning.Receipts:
    shas: dict[str, str] = {}
    census = None
    census_sha = None
    if spec.census is not None:
        raw, census_sha = _read_bound(spec.census)
        body = strict_json(raw, "invalid_census")
        census = attr.parse_census(
            body.get("gl_attribution", body) if isinstance(body, dict) else {},
            census_sha,
            body.get("census_sha256") if isinstance(body, dict) else None,
        )
        shas["census"] = census_sha
    atts = {}
    for code, ref in sorted(spec.gl_attribution.items()):
        raw, sha = _read_bound(ref)
        att, _ = attr.parse_gl_attribution(raw)
        if att.code != code:
            refuse("invalid_gl_attribution", "receipt code differs from the spec")
        atts[code] = att
        shas[f"gl_attribution:{code}"] = sha
    return planning.Receipts(census, census_sha, atts, shas)


def _engine():
    return cli_common.engine()


def _operator(value: str | None) -> str:
    return cli_common.operator(value)


def _plan_receipt(path: Path, sha: str) -> dict[str, Any]:
    sha_text(sha, "plan_sha_required")
    body = json.loads(path.read_bytes())
    if not isinstance(body, dict) or body.get("plan_sha256") != sha:
        refuse("plan_receipt_mismatch")
    text(body.get("planned_by"), "operator_ref_required")
    return body


def _do_plan(args) -> dict[str, Any]:
    spec_bytes = args.spec.read_bytes()
    out = applying.run_plan(
        _engine(), spec_bytes, load_receipts, billing_reader(), planned_by=_operator(args.operator_ref)
    )
    return {"ready": True, "dry_run": True, **out}


def _do_apply(args) -> dict[str, Any]:
    _require_apply(args.apply)
    spec_bytes = args.spec.read_bytes()
    spec, _ = planning.parse_spec(spec_bytes)
    approved = _plan_receipt(args.plan_receipt, args.plan_sha)
    engine = _engine()
    operator = _operator(args.operator_ref)
    with Session(engine) as db:
        from erp_v2.journals.policy import functional_currency

        prior = applying.applied_fingerprints(
            db, spec.tenant_id, spec.window_id, functional_currency(db, spec.tenant_id)
        )
        db.rollback()
    if prior and any(fp != args.plan_sha for fp in prior.values()):
        refuse("window_already_applied", "this window was applied from a different plan")
    if prior:
        return {"ready": True, "replayed": True, "plan_sha256": args.plan_sha}
    plan = applying.run_plan(engine, spec_bytes, load_receipts, billing_reader(), planned_by=approved["planned_by"])
    with Session(engine) as db, db.begin():
        return applying.apply(db, spec, plan, approved_sha=args.plan_sha, applied_by=operator)


def _do_verify(args) -> dict[str, Any]:
    spec, _ = planning.parse_spec(args.spec.read_bytes())
    engine = _engine()
    with applying.read_only_session(engine) as db:
        out = applying.verify(db, spec, billing_reader())
        db.rollback()
    return out


def _do_t1_plan(args) -> dict[str, Any]:
    out = t1_mod.run_t1_plan(
        _engine(), args.spec.read_bytes(), load_receipts, billing_reader(), planned_by=_operator(args.operator_ref)
    )
    return {"ready": True, "dry_run": True, **out}


def _do_t1_apply(args) -> dict[str, Any]:
    _require_apply(args.apply)
    spec_bytes = args.spec.read_bytes()
    spec, _ = planning.parse_spec(spec_bytes)
    approved = _plan_receipt(args.plan_receipt, args.plan_sha)
    engine = _engine()
    plan = t1_mod.run_t1_plan(engine, spec_bytes, load_receipts, billing_reader(), planned_by=approved["planned_by"])
    if plan["plan"]["already_posted"]:
        if plan["plan"]["posted_fingerprint"] != args.plan_sha:  # L1: a replay needs the posted journal's fingerprint
            refuse("t1_already_posted_by_another_plan")
        return {"ready": True, "replayed": True, "plan_sha256": args.plan_sha}
    with Session(engine) as db, db.begin():
        return t1_mod.apply_t1(db, spec, plan, approved_sha=args.plan_sha, applied_by=_operator(args.operator_ref))


def _straddle_raw(spec: planning.RunSpec) -> bytes:
    if spec.straddle is None:
        refuse("straddle_file_required")
    return _read_bound(spec.straddle)[0]


def _do_straddle(args) -> dict[str, Any]:
    from erp_v2.holdings import straddle

    spec_bytes = args.spec.read_bytes()
    spec, _ = planning.parse_spec(spec_bytes)
    engine = _engine()
    if args.mode == "plan":
        return {
            "ready": True,
            "dry_run": True,
            **straddle.run_plan(engine, spec_bytes, planned_by=_operator(args.operator_ref)),
        }
    if args.mode == "verify":
        with applying.read_only_session(engine) as db:
            out = straddle.verify(db, spec, _straddle_raw(spec))
            db.rollback()
        return out
    _require_apply(args.apply)
    approved = _plan_receipt(args.plan_receipt, args.plan_sha)
    plan = straddle.run_plan(engine, spec_bytes, planned_by=approved["planned_by"])
    with Session(engine) as db, db.begin():
        return straddle.apply(db, spec, plan, approved_sha=args.plan_sha, applied_by=_operator(args.operator_ref))


def _command(args) -> dict[str, Any]:
    raw = args.owner_command.read_bytes()
    body = strict_json(raw, "invalid_owner_command")
    if not isinstance(body, dict):
        refuse("invalid_owner_command")
    return body


def _do_release(args) -> dict[str, Any]:
    _require_apply(args.apply)
    spec, _ = planning.parse_spec(args.spec.read_bytes())
    out = releasing.release(
        _engine(), spec.tenant_id, args.exception, _command(args), operator=_operator(args.operator_ref)
    )
    return out


def _do_writeoff(args) -> dict[str, Any]:
    spec, _ = planning.parse_spec(args.spec.read_bytes())
    allow = tuple(spec.params.get("migration.writeoff_offset_codes", []))
    operator = _operator(args.operator_ref)
    engine = _engine()
    if args.mode == "plan":
        with Session(engine) as db:
            out = releasing.plan_writeoff(
                db, spec.tenant_id, args.exception, args.offset_account, args.evidence_sha,
                allowlist=allow, planned_by=operator,
            )  # fmt: skip
            db.rollback()
        return {"ready": True, "dry_run": True, **out}
    _require_apply(args.apply)
    approved = _plan_receipt(args.plan_receipt, args.plan_sha)
    with Session(engine) as db, db.begin():
        plan = releasing.plan_writeoff(
            db, spec.tenant_id, args.exception, args.offset_account, args.evidence_sha,
            allowlist=allow, planned_by=approved["planned_by"],
        )  # fmt: skip
        return releasing.apply_writeoff(
            db, spec.tenant_id, args.exception, plan, approved_sha=args.plan_sha, applied_by=operator,
            on_date=datetime.now(UTC).date(),
        )  # fmt: skip


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="erp_v2.holdings.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, *, apply: bool = False, plan_bound: bool = False):
        sp.add_argument("--spec", type=Path, required=True)
        sp.add_argument("--receipt", type=Path, required=True)
        sp.add_argument("--operator-ref")
        if apply:
            sp.add_argument("--apply", action="store_true")
        if plan_bound:
            sp.add_argument("--plan-receipt", type=Path, required=True)
            sp.add_argument("--plan-sha", required=True)

    common(sub.add_parser("plan"))
    common(sub.add_parser("apply"), apply=True, plan_bound=True)
    common(sub.add_parser("verify"))
    t1 = sub.add_parser("t1").add_subparsers(dest="mode", required=True)
    common(t1.add_parser("plan"))
    common(t1.add_parser("apply"), apply=True, plan_bound=True)
    st = sub.add_parser("straddle").add_subparsers(dest="mode", required=True)
    common(st.add_parser("plan"))
    common(st.add_parser("verify"))
    common(st.add_parser("apply"), apply=True, plan_bound=True)
    rel = sub.add_parser("release")
    common(rel, apply=True)
    rel.add_argument("--exception", required=True)
    rel.add_argument("--owner-command", type=Path, required=True)
    wo = sub.add_parser("writeoff").add_subparsers(dest="mode", required=True)
    for mode in ("plan", "apply"):
        sp = wo.add_parser(mode)
        common(sp, apply=mode == "apply")
        sp.add_argument("--exception", required=True)
        sp.add_argument("--offset-account", required=True)
        sp.add_argument("--evidence-sha", required=True)
        if mode == "apply":
            sp.add_argument("--plan-receipt", type=Path, required=True)
            sp.add_argument("--plan-sha", required=True)
    return p


_HANDLERS = {
    ("plan", None): _do_plan,
    ("apply", None): _do_apply,
    ("verify", None): _do_verify,
    ("t1", "plan"): _do_t1_plan,
    ("t1", "apply"): _do_t1_apply,
    ("straddle", "plan"): _do_straddle,
    ("straddle", "apply"): _do_straddle,
    ("straddle", "verify"): _do_straddle,
    ("release", None): _do_release,
    ("writeoff", "plan"): _do_writeoff,
    ("writeoff", "apply"): _do_writeoff,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handler = _HANDLERS[(args.cmd, getattr(args, "mode", None))]
    label = f"holding {args.cmd}" + (f" {args.mode}" if getattr(args, "mode", None) else "")

    return cli_common.run(args.receipt, lambda: handler(args), label)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
