"""``python -m erp_v2.billing_gl run --tenant-id <uuid> [--apply] [--enable-once]`` (1-minute schedule).

Runs as the RUNTIME role: ``DATABASE_URL`` only (the migration and legacy DSNs are refused). Dry-run by default:
without ``--apply`` AND ``ERP_BILLING_GL_APPLY=1`` the run executes in one transaction that is ROLLED BACK, so it
reports what would happen and writes nothing. The ``billing_gl.enabled`` setting (default false) keeps the mapper
idle in production; ``--enable-once`` runs ONE pass with the flag still false: it dispositions every fact (opening
facts become SKIP for the flip gate) but never posts a journal. No secret, tenant id or amount is printed.

    {"mode": "dry-run"|"apply", ...counts by reason...}

Exit codes: 0 ok, 2 refused, 3 integrity alarm (a fact digest changed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from uuid import UUID

from erp_v2.billing_gl.feature import AUDIT_RUN

APPLY_ENV = "ERP_BILLING_GL_APPLY"


def _refuse(code: str) -> int:
    print(json.dumps({"refused": code}))
    return 2


def _engine():
    import sqlalchemy as sa

    url = os.environ.pop("DATABASE_URL", "")
    if not url:
        return None
    eng = sa.create_engine(url, echo=False, hide_parameters=True)
    return eng if eng.dialect.name == "postgresql" else None


def run_once(db, tenant_id: UUID, *, apply: bool, enable_once: bool, now: datetime | None = None) -> dict:
    """One pass in an already-open session; commits only when ``apply``. Returns the report."""
    import sqlalchemy as sa

    from erp_v2.billing_gl.mapper import Mapper

    db.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(tenant_id)})
    mapper = Mapper(
        db,
        tenant_id,
        now=now or datetime.now(UTC),
        commit=db.commit if apply else (lambda: None),
        enable_once=enable_once,
    )
    try:
        report = mapper.run()
    except BaseException:
        db.rollback()
        raise
    summary = report.as_dict()
    if apply and (report.posted or report.blocked or report.waiting or report.windows_closed):
        _audit(db, tenant_id, AUDIT_RUN, summary)
    if apply:
        db.commit()
    else:
        db.rollback()
    return summary


def _audit(db, tenant_id: UUID, action: str, details: dict) -> None:
    from dotmac_kernel.audit import write_audit_event

    write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_type="system",
        actor_id="erp.billing_gl",
        source_application="dotmac_erp",
        action=action,
        entity_type="billing_gl_run",
        is_success=action == AUDIT_RUN,
        details={k: v for k, v in details.items() if isinstance(v, int | str | bool)},
    )


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in[:1] == ["vat-opening"]:
        from erp_v2.billing_gl import vat_opening

        return vat_opening.main(args_in[1:])
    parser = argparse.ArgumentParser(prog="erp_v2.billing_gl")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--tenant-id", required=True)
    run.add_argument("--apply", action="store_true")
    run.add_argument("--enable-once", action="store_true")
    args = parser.parse_args(argv)
    try:
        tenant = UUID(args.tenant_id)
    except ValueError:
        return _refuse("invalid_tenant_id")
    if "MIGRATION_DATABASE_URL" in os.environ or "LEGACY_DATABASE_URL" in os.environ:
        return _refuse("operator_database_materials_forbidden")
    apply = args.apply and os.environ.get(APPLY_ENV) == "1"
    if args.apply and not apply:
        return _refuse("explicit_billing_gl_apply_required")
    engine = _engine()
    if engine is None:
        return _refuse("runtime_database_required")
    from sqlalchemy.orm import Session

    from erp_v2.billing_gl.mapper import BillingFactDigestChanged

    try:
        with Session(engine) as db:
            report = run_once(db, tenant, apply=apply, enable_once=args.enable_once)
    except BillingFactDigestChanged:
        print(json.dumps({"refused": "billing_fact_digest_changed", "integrity_alarm": True}))
        return 3
    except Exception as exc:
        print(json.dumps({"refused": "billing_gl_run_failed", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps({"mode": "apply" if apply else "dry-run", **report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
