"""Operator plan/apply of a sha-bound Finance decision file: reviewed admission (B-1, D-M3, D-M5).

Finance decides, per legacy invoice, how an exception is resolved, with evidence. The decisions are recorded
here, append-only, in ``erp_admission_reviews`` (the runtime role has SELECT only; this command is the only
writer). The verifiers (B1b recognition, settled admission, historical unpaid admission) read the row and honour
exactly what it says; nothing else changes about them.

Decision file (JSON, at most 1000 rows, canonical lower-case UUIDs, duplicate keys refused)::

    {"version": 1, "tenant_id": "<uuid>", "snapshot_sha256": "<64 hex>", "decision_ref": "<text, <=120>",
     "rows": [{"legacy_invoice_id": "<uuid>", "decision": "<DECISIONS>", "waived_checks": ["<name>"],
               "evidence_sha": "<64 hex: the Finance decision or third-party document>",
               "decided_by": "<Finance person, <=120>", "decided_at": "<ISO timestamp with offset>",
               "ias8_account_id": "<uuid of account 3100>"   <- only for ias8_post}]}

* ``admit_with_waivers`` names exactly ONE check (D-M3) from the allowlist in ``erp_v2.importer.admission_waivers``;
  the GL tie, the integrity checks, tenant, snapshot binding, fingerprint, currency and duplicate admission are not
  waivable and a file naming them is refused whole.
* ``ias8_post`` (D-M5) carries the account the expense side is posted to, which must be the tenant's account 3100.
* The file is bound by ``--decision-sha256`` (recomputed over the bytes) and by ``--snapshot-sha256``.
* The dual-control policy (default OFF, ``erp_v2.governance``) is read per tenant and stamped on each row: when it
  is ON the applying operator must differ from the person who decided each row; when OFF one person may do both.
* A row identical to a recorded one is a no-op; a different decision for an invoice already decided is refused
  (a changed decision needs a new ruling, not an edit).

    python -m erp_v2.legacy_ap.review_cli --decisions D.json --decision-sha256 S --snapshot-sha256 N \
        --receipt R.json [--apply --operator-ref <who>]

Apply needs ``--apply``, ERP_IMPORT_APPROVED=1 and ERP_IMPORT_APPLY_OK=1. The DSN is environment-only
(MIGRATION_DATABASE_URL). The 0600 receipt, created exclusively before any work, carries counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from erp_v2.importer.admission_waivers import (
    DECISIONS,
    IAS8_ACCOUNT_CODE,
    MAX_WAIVERS_PER_INVOICE,
    WAIVABLE_CHECKS,
)

MAX_ROWS = 1000
_SHA = re.compile(r"[0-9a-f]{64}")
_ROW_KEYS = {"legacy_invoice_id", "decision", "waived_checks", "evidence_sha", "decided_by", "decided_at"}


class ReviewRefusal(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ReviewRefusal(reason)


def _uuid(value, reason: str) -> UUID:
    try:
        ident = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReviewRefusal(reason) from exc
    _require(str(ident) == value, reason)
    return ident


def _text(value, reason: str, maximum: int) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()) and value == value.strip() and len(value) <= maximum, reason
    )
    return value


@dataclass(frozen=True, slots=True)
class DecisionRow:
    legacy_invoice_id: UUID
    decision: str
    waived: tuple[str, ...]
    evidence_sha: str
    decided_by: str
    decided_at: datetime
    ias8_account_id: UUID | None


@dataclass(frozen=True, slots=True)
class DecisionFile:
    tenant_id: UUID
    snapshot_sha256: str
    decision_ref: str
    sha256: str
    rows: tuple[DecisionRow, ...]


def _json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except ReviewRefusal:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReviewRefusal("invalid_json_document") from exc
    _require(isinstance(value, dict), "json_object_required")
    return value


def parse_decision_file(raw: bytes, expected_sha256: str, snapshot_sha256: str) -> DecisionFile:
    """Validate the sha256-bound decision file. Every refusal refuses the whole file (no partial acceptance)."""
    _require(bool(_SHA.fullmatch(expected_sha256 or "")), "decision_sha256_required")
    _require(hashlib.sha256(raw).hexdigest() == expected_sha256, "decision_digest_mismatch")
    _require(bool(_SHA.fullmatch(snapshot_sha256 or "")), "snapshot_sha256_required")
    data = _json(raw)
    _require(
        set(data) == {"version", "tenant_id", "snapshot_sha256", "decision_ref", "rows"}, "invalid_decision_schema"
    )
    _require(type(data["version"]) is int and data["version"] == 1, "invalid_decision_version")
    _require(data["snapshot_sha256"] == snapshot_sha256, "decision_snapshot_mismatch")
    tenant = _uuid(data["tenant_id"], "invalid_tenant_id")
    ref = _text(data["decision_ref"], "decision_ref_required", 120)
    rows = data["rows"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= MAX_ROWS, "explicit_bounded_rows_required")
    parsed = []
    for row in rows:
        _require(isinstance(row, dict), "invalid_row")
        decision = row.get("decision")
        _require(decision in DECISIONS, "unknown_decision")
        _require(
            set(row) == _ROW_KEYS | ({"ias8_account_id"} if decision == "ias8_post" else set()), "invalid_row_schema"
        )
        invoice = _uuid(row["legacy_invoice_id"], "invalid_invoice_id")
        waived = row["waived_checks"]
        _require(isinstance(waived, list) and all(isinstance(name, str) for name in waived), "invalid_waived_checks")
        _require(len(set(waived)) == len(waived), "duplicate_waived_check")
        # Closed vocabulary: anything outside the allowlist (the GL tie included) refuses the file.
        _require(all(name in WAIVABLE_CHECKS for name in waived), "check_not_waivable")
        _require(len(waived) <= MAX_WAIVERS_PER_INVOICE, "too_many_waivers")
        _require((decision == "admit_with_waivers") == bool(waived), "waivers_do_not_match_decision")
        _require(
            bool(_SHA.fullmatch(row["evidence_sha"] if isinstance(row["evidence_sha"], str) else "")),
            "evidence_sha_required",
        )
        decided_by = _text(row["decided_by"], "decided_by_required", 120)
        try:
            decided_at = datetime.fromisoformat(row["decided_at"])
        except (ValueError, TypeError) as exc:
            raise ReviewRefusal("invalid_decided_at") from exc
        _require(decided_at.tzinfo is not None and decided_at.utcoffset() is not None, "invalid_decided_at")
        account = _uuid(row["ias8_account_id"], "invalid_ias8_account_id") if decision == "ias8_post" else None
        parsed.append(
            DecisionRow(invoice, decision, tuple(waived), row["evidence_sha"], decided_by, decided_at, account)
        )
    _require(len({row.legacy_invoice_id for row in parsed}) == len(parsed), "duplicate_invoice_id")
    return DecisionFile(
        tenant, snapshot_sha256, ref, expected_sha256, tuple(sorted(parsed, key=lambda r: str(r.legacy_invoice_id)))
    )


def _same(row: DecisionRow, record, snapshot_sha256: str) -> bool:
    return (
        record.decision == row.decision
        and tuple(record.waived_checks) == row.waived
        and record.evidence_sha == row.evidence_sha
        and record.snapshot_sha256 == snapshot_sha256
        and record.decided_by == row.decided_by
        and (None if record.ias8_account_id is None else str(record.ias8_account_id))
        == (None if row.ias8_account_id is None else str(row.ias8_account_id))
    )


def classify(db, file: DecisionFile, *, operator_ref: str, dual: bool) -> tuple[list[tuple[DecisionRow, str]], Counter]:
    """Per row: 'new' | 'already_recorded' | a refusal reason. Read-only."""
    import sqlalchemy as sa
    from dotmac_accounting.models import Account
    from dotmac_payables.models import SupplierInvoice

    from erp_v2.legacy_ap.models import AdmissionReview

    existing = {
        r.legacy_invoice_id: r
        for r in db.scalars(sa.select(AdmissionReview).where(AdmissionReview.tenant_id == file.tenant_id))
    }
    known = set(
        db.scalars(
            sa.select(SupplierInvoice.id).where(
                SupplierInvoice.tenant_id == file.tenant_id,
                SupplierInvoice.id.in_([r.legacy_invoice_id for r in file.rows]),
            )
        )
    )
    outcomes: list[tuple[DecisionRow, str]] = []
    for row in file.rows:
        record = existing.get(row.legacy_invoice_id)
        if record is not None:
            outcomes.append(
                (row, "already_recorded" if _same(row, record, file.snapshot_sha256) else "different_decision_recorded")
            )
            continue
        if row.legacy_invoice_id not in known:
            outcomes.append((row, "invoice_not_found"))
            continue
        if dual and row.decided_by == operator_ref:
            outcomes.append((row, "dual_control_operator_is_decider"))
            continue
        if row.ias8_account_id is not None:
            account = db.scalar(
                sa.select(Account).where(Account.tenant_id == file.tenant_id, Account.id == row.ias8_account_id)
            )
            if account is None or account.code != IAS8_ACCOUNT_CODE:
                outcomes.append((row, "ias8_account_not_3100"))
                continue
            if not account.is_active or not account.posting_allowed:
                outcomes.append((row, "ias8_account_not_postable"))
                continue
        outcomes.append((row, "new"))
    return outcomes, Counter(reason for _row, reason in outcomes)


def receipt_of(file: DecisionFile, counts: Counter, *, mode: str, dual: bool, written: int = 0) -> dict:
    """Counts and digests only: never invoice ids, names or other personal data."""
    refused = {k: v for k, v in counts.items() if k not in ("new", "already_recorded")}
    return dict(
        mode=mode,
        production_ready=False,
        decision_sha256=file.sha256,
        snapshot_sha256=file.snapshot_sha256,
        dual_control=dual,
        counts=dict(
            requested=len(file.rows),
            new=counts.get("new", 0),
            already_recorded=counts.get("already_recorded", 0),
            refused=sum(refused.values()),
        ),
        refusals_by_reason=dict(sorted(refused.items())),
        by_decision=dict(sorted(Counter(row.decision for row in file.rows).items())),
        written=written,
    )


def apply_reviews(db, file: DecisionFile, outcomes, *, operator_ref: str, dual: bool) -> int:
    """Write every new row and its audit event in the caller's transaction (never commits)."""
    from dotmac_kernel.audit import write_audit_event

    from erp_v2.legacy_ap.audit import AUDIT_REVIEW_RECORDED, SOURCE_APPLICATION
    from erp_v2.legacy_ap.models import AdmissionReview

    _require(
        all(reason in ("new", "already_recorded") for _row, reason in outcomes), "decision_file_not_fully_eligible"
    )
    written = 0
    for row, reason in outcomes:
        if reason != "new":
            continue
        review = AdmissionReview(
            id=uuid4(),
            tenant_id=file.tenant_id,
            legacy_invoice_id=row.legacy_invoice_id,
            decision=row.decision,
            waived_checks=list(row.waived),
            evidence_sha=row.evidence_sha,
            decision_file_sha=file.sha256,
            snapshot_sha256=file.snapshot_sha256,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            ias8_account_id=row.ias8_account_id,
            operator_ref=operator_ref,
            dual_control=dual,
        )
        db.add(review)
        db.flush()
        write_audit_event(
            db,
            tenant_id=file.tenant_id,
            actor_type="service",
            actor_id=operator_ref,
            actor_label="erp_v2.legacy_ap.review_cli",
            source_application=SOURCE_APPLICATION,
            action=AUDIT_REVIEW_RECORDED,
            entity_type="supplier_invoice",
            entity_id=str(row.legacy_invoice_id),
            is_success=True,
            details={
                "review_id": str(review.id),
                "decision": row.decision,
                "waived_checks": list(row.waived),
                "evidence_sha": row.evidence_sha,
                "decision_file_sha": file.sha256,
                "snapshot_sha256": file.snapshot_sha256,
                "decided_by": row.decided_by,
                "dual_control": dual,
            },
        )
        written += 1
    return written


def run(db, file: DecisionFile, *, apply: bool, operator_ref: str) -> dict:
    """Core of both modes; the caller owns the transaction (plan is rolled back by the caller)."""
    import sqlalchemy as sa

    from erp_v2.governance.policy import dual_control

    db.execute(sa.text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(file.tenant_id)})
    _require(
        db.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM public.tenants WHERE id = :t AND is_active)"), {"t": file.tenant_id}
        )
        is True,
        "tenant_unknown",
    )
    dual = dual_control(db, file.tenant_id)
    outcomes, counts = classify(db, file, operator_ref=operator_ref, dual=dual)
    if not apply:
        return receipt_of(file, counts, mode="plan", dual=dual)
    written = apply_reviews(db, file, outcomes, operator_ref=operator_ref, dual=dual)
    return receipt_of(file, counts, mode="apply", dual=dual, written=written)


def _authorized(args) -> bool:
    return not args.apply or (
        os.environ.get("ERP_IMPORT_APPROVED") == "1" and os.environ.get("ERP_IMPORT_APPLY_OK") == "1"
    )


def _run(args) -> dict:
    url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    _require(_authorized(args), "explicit_apply_authorization_required")
    _require(bool(url), "operator_database_materials_required")
    _require(
        "DATABASE_URL" not in os.environ and "PLATFORM_DATABASE_URL" not in os.environ,
        "serving_database_materials_forbidden",
    )
    operator_ref = (args.operator_ref or "").strip()
    _require(not args.apply or (bool(operator_ref) and len(operator_ref) <= 120), "operator_ref_required")
    file = parse_decision_file(args.decisions.read_bytes(), args.decision_sha256, args.snapshot_sha256)
    import sqlalchemy as sa
    from dotmac_kernel.audit_actions import (
        AuditActionRegistry,
        AuditActionsNotInstalledError,
        active_audit_actions,
        install_audit_actions,
    )
    from sqlalchemy.orm import Session

    try:
        active_audit_actions()
    except AuditActionsNotInstalledError:
        from erp_v2.assembly import assembly
        from erp_v2.legacy_ap.audit import legacy_recognize_feature

        install_audit_actions(AuditActionRegistry.from_manifests([*assembly.modules, legacy_recognize_feature]))
    engine = sa.create_engine(url, echo=False, hide_parameters=True)
    try:
        _require(engine.dialect.name == "postgresql", "postgresql_required")
        with Session(engine) as db, db.begin():
            if not args.apply:
                db.execute(sa.text("SET TRANSACTION READ ONLY"))
            return run(db, file, apply=args.apply, operator_ref=operator_ref)
    finally:
        engine.dispose()


def _receipt(path: Path) -> int:
    _require(path.parent.resolve(strict=True) == Path(os.path.abspath(path.parent)), "receipt_symlink_forbidden")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.legacy_ap.review_cli")
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--decision-sha256", required=True)
    parser.add_argument("--snapshot-sha256", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--operator-ref")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        _require(_authorized(args), "explicit_apply_authorization_required")
        fd = _receipt(args.receipt)
    except Exception:
        print("admission review refused: authorization or private receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = _run(args)
            code = 0 if payload["counts"]["refused"] == 0 else 3
        except ReviewRefusal as exc:
            payload, code = dict(production_ready=False, refusal=exc.reason), 2
        except Exception:
            payload, code = dict(production_ready=False, refusal="admission_review_command_refused"), 2
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    counts = payload.get("counts", {})
    print(
        "admission review: "
        + (
            f"requested={counts.get('requested', 0)} new={counts.get('new', 0)} "
            f"already_recorded={counts.get('already_recorded', 0)} refused={counts.get('refused', 0)}"
            if counts
            else "command refused"
        )
        + "; production_ready=false"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
