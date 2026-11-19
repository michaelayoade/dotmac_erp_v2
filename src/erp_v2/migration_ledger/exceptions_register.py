"""Operator tool: load, reconcile, resolve and gate the exceptions register (B-9).

    python -m erp_v2.migration_ledger.exceptions_register load --spec S.json --receipt R.json --operator-ref <who> \
        [--apply]
    python -m erp_v2.migration_ledger.exceptions_register resolve --spec S.json --class E16a --key <legacy key> \
        --outcome <posted|admitted|voided|written_off|no_action> --evidence-sha256 <sha> --receipt R.json \
        --operator-ref <who> --apply
    python -m erp_v2.migration_ledger.exceptions_register add --spec S.json --class E26 --key <legacy key> \
        --evidence-sha256 <sha> --receipt R.json --operator-ref <who> --apply
    python -m erp_v2.migration_ledger.exceptions_register declare-na --spec S.json --class E27 --reason <text> \
        --evidence-sha256 <sha> --receipt R.json --operator-ref <who> --apply
    python -m erp_v2.migration_ledger.exceptions_register gate --spec S.json --receipt R.json --operator-ref <who>

``S.json`` is ``{"version": 1, "tenant_id": "<uuid>", "receipts": [{"kind": "held_drafts"|"import_itemised",
"path": "<file>", "sha256": "<sha of the file>"}], "include_admission_reviews": true}``; every receipt is read
through its sha and loaded by its adapter. Dry run is the default; every write needs ``--apply`` AND
``ERP_MIGRATION_LEDGER_APPLY=1``. ``gate`` is read-only: exit 0 only when the open count is 0 AND the latest bridge
version is signed; exit 3 otherwise (with the reasons).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.migration_ledger import cli_common as cli
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import keys_exactly, refuse, sha_text, strict_json, text, uuid_text

_SPEC_KEYS = {"version", "tenant_id", "receipts", "include_admission_reviews"}


def load_spec(path: Path):
    data, _sha = cli.read_bound(path)
    spec = keys_exactly(strict_json(data, "invalid_spec_json"), _SPEC_KEYS, "invalid_spec_schema")
    if (
        spec["version"] != 1
        or not isinstance(spec["receipts"], list)
        or not isinstance(spec["include_admission_reviews"], bool)
    ):
        refuse("invalid_spec_schema")
    return uuid_text(spec["tenant_id"], "invalid_tenant_id"), spec


def gather(db: Session, tenant, spec) -> tuple[list[register.Fact], set[str], str]:
    facts: list[register.Fact] = []
    sources: set[str] = set()
    shas: list[str] = []
    for entry in spec["receipts"]:
        e = keys_exactly(entry, {"kind", "path", "sha256"}, "invalid_spec_schema")
        if e["kind"] not in register.RECEIPT_KINDS:
            refuse("unknown_receipt_kind")
        data, sha = cli.read_bound(
            Path(text(e["path"], "invalid_spec_schema", 1024)), sha_text(e["sha256"], "invalid_spec_schema")
        )
        body = strict_json(data, "invalid_receipt_json")
        facts += register.from_import_itemised(body)
        sources.add(e["kind"])
        shas.append(sha)
    facts += register.from_held_decisions(db, tenant)  # B-2 outcomes: the immutable table, never a receipt file
    sources.add(register.SRC_HELD)
    if spec["include_admission_reviews"]:
        facts += register.from_admission_reviews(db, tenant)
        sources.add(register.SRC_REVIEWS)
    from erp_v2.migration_ledger.common import canonical_sha

    return facts, sources, canonical_sha(sorted(shas))


def _run(args) -> dict[str, Any]:
    operator = cli.operator(args.operator_ref)
    tenant, spec = load_spec(args.spec)
    eng = cli.engine()  # the DSN leaves the environment BEFORE the assembly is imported
    cli.install_registries()
    try:
        with Session(eng) as db:
            if not db.scalar(sa.text("SELECT is_active FROM tenants WHERE id = :t"), {"t": tenant}):
                refuse("tenant_missing_or_inactive")
            if args.mode == "gate":
                db.execute(sa.text("SET TRANSACTION READ ONLY"))
                result = register.gate(db, tenant)
                db.rollback()
                return {"mode": "gate", "ready": result["passed"], **result}
            if args.mode == "load":
                facts, sources, receipts_sha = gather(db, tenant, spec)
                if args.apply:
                    cli.require_apply(args.apply)
                counts = register.reconcile(
                    db, tenant, facts, loaded_sources=sources, receipt_sha=receipts_sha, apply=args.apply
                )
                out = {
                    "mode": "load",
                    "applied": bool(args.apply),
                    "facts": len(facts),
                    "reconcile": counts,
                    "register": register.counts(db, tenant),
                    "operator": operator,
                }
                if args.apply:
                    db.commit()
                else:
                    db.rollback()
                return out
            cli.require_apply(args.apply)
            if args.mode in ("add", "declare-na"):
                if args.mode == "add":
                    row = register.add_item(
                        db,
                        tenant,
                        class_code=args.class_code,
                        legacy_key=args.key or "",
                        evidence_sha256=args.evidence_sha256 or "",
                        operator=operator,
                    )
                else:
                    row = register.declare_na(
                        db,
                        tenant,
                        class_code=args.class_code,
                        reason=args.reason or "",
                        evidence_sha256=args.evidence_sha256 or "",
                        operator=operator,
                    )
                db.commit()
                return {
                    "mode": args.mode,
                    "class": row.class_code,
                    "state": row.state,
                    "register": register.counts(db, tenant),
                }
            row = register.resolve(
                db,
                tenant,
                class_code=text(args.class_code, "class_required", 20),
                legacy_key=text(args.key, "key_required", 255),
                outcome=args.outcome or "",
                evidence_sha256=args.evidence_sha256 or "",
            )
            db.commit()
            return {
                "mode": "resolve",
                "state": row.state,
                "outcome": row.terminal_outcome,
                "register": register.counts(db, tenant),
            }
    finally:
        eng.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.migration_ledger.exceptions_register")
    parser.add_argument("mode", choices=("load", "resolve", "gate", "add", "declare-na"))
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--operator-ref", required=True)
    parser.add_argument("--class", dest="class_code")
    parser.add_argument("--key")
    parser.add_argument("--outcome")
    parser.add_argument("--evidence-sha256")
    parser.add_argument("--reason", help="declare-na: why the class does not apply (<= 500 characters)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "add" and not (args.class_code and args.key and args.evidence_sha256):
        parser.error("add needs --class, --key and --evidence-sha256")
    if args.mode == "declare-na" and not (args.class_code and args.reason and args.evidence_sha256):
        parser.error("declare-na needs --class, --reason and --evidence-sha256")
    if args.mode == "resolve" and not (args.class_code and args.key and args.outcome and args.evidence_sha256):
        parser.error("resolve needs --class, --key, --outcome and --evidence-sha256")
    return cli.run(
        args.receipt,
        lambda: _run(args),
        "exceptions register",
        pre=(lambda: cli.require_apply(args.apply)) if args.mode in ("resolve", "add", "declare-na") else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
