"""Operator CLI: SETTLED historical admission of legacy partially paid supplier invoices.

Michael's ruling: "lets migrate them into their accounting settled ledgers". Per invoice, in ONE
savepoint of ONE operator transaction, ``payables.admit_historical_settled_invoice`` (0.1.0a5):

    full-face obligation + one immutable historical_settlements row per legacy payment allocation
    (each citing the legacy payment journal and its imported destination journal) + liability events;
    the outstanding amount is exactly total - paid. No accounting consequence is produced: legacy AP
    control already holds the residual (the verifier proves it, see ``settled_verifier``).

Amounts are never changed or recomputed: after each admission the landed obligation and settlement rows
are asserted equal to the reviewed plan. Sequencing: the unpaid import (wave 3) and B1b run first; the
inherited destination preflight requires every payment table to be empty, but ``historical_settlements``
is the table this command writes, so for this command only it is governed differently: rows may exist
only for invoices of the list being run (a re-run is a no-op; settlements of any other invoice refuse).
Supplier payments, allocations, withholdings and settlement observations must still be empty.

Run order is enforced: ``--apply`` needs ``--unpaid-receipt``/``--unpaid-receipt-sha256`` (the wave-3 unpaid
import apply receipt) and ``--b1b-receipt``/``--b1b-receipt-sha256`` (the B1b apply receipt). Each is checked
(file, digest, kind, nothing refused, same tenant and snapshot) and its digest is bound into the plan, hence
into the reviewed-plan digest; ``prerequisite_receipt_missing`` / ``prerequisite_receipt_mismatch`` otherwise.
Plan mode may run without them and says they will be required (the reviewed plan must then be re-planned with
them). The apply lock is per tenant.

Input list (JSON, produced from the step-3 census category R3; this command only validates it), pinned
by ``--list-sha256``: same schema as the B1b list (``recognize_cli.parse_list``).

Plan mode (default) runs in a READ ONLY transaction and writes nothing. Apply needs ``--apply``,
``--reviewed-plan-sha256`` (recomputed and compared), ERP_IMPORT_APPROVED=1 + ERP_IMPORT_APPLY_OK=1, and
refuses unless every id is eligible or already done. Refused (named reason codes): not APPROVED, existing
owner state, a different historical receipt, a settlement id already admitted or duplicated across the
list, re-entry under a legacy reference, or any legacy-evidence failure (unposted payment or payment
journal, sum mismatch, non-NGN, procurement links, ...). The receipt carries counts, reasons-by-count and
money totals plus digests only: no invoice, payment or settlement ids. The private plan file (0600) holds
the per-invoice detail. The legacy and migration DSNs come from LEGACY_DATABASE_URL and
MIGRATION_DATABASE_URL and are removed from the environment before use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer.payables_history import admission_notes
from erp_v2.legacy_ap.recognize_cli import (
    FINANCE_APPROVER,
    RecognizeRefusal,
    _authorized,
    _dump,
    _install_audit_actions,
    _json,
    _money,
    _receipt,
    _require,
    _uuid,
    parse_list,
)

REQUIRED_PAYABLES_VERSION = "0.1.0a5"
SOURCE_OWNER = "legacy-erp"
DOCUMENT_KIND = "supplier_invoice"
_REASON = re.compile(r"[a-z][a-z0-9_:.-]*")
SettledRefusal = RecognizeRefusal


def _identity_key(invoice_id: UUID, snapshot_sha256: str) -> str:
    identity = json.dumps(
        dict(source_owner=SOURCE_OWNER, source_invoice_id=str(invoice_id), source_snapshot_sha256=snapshot_sha256),
        sort_keys=True,
        separators=(",", ":"),
    )
    return "erp.settled-admission:" + hashlib.sha256(identity.encode()).hexdigest()


PREREQUISITES = ("unpaid_receipt_sha256", "b1b_receipt_sha256")


def _prerequisite(path, sha, *, kind: str, tenant: UUID, manifest) -> str:
    """Verify one prerequisite apply receipt (a file the operator holds); return its sha256.

    The unpaid-import receipt is ``payables_history_cli`` apply output, the B1b receipt is
    ``recognize_cli`` apply output. Each must be an apply receipt of its own kind with nothing refused,
    for this tenant and this snapshot."""
    _require(path is not None and bool(re.fullmatch(r"[0-9a-f]{64}", sha or "")), "prerequisite_receipt_missing")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise RecognizeRefusal("prerequisite_receipt_missing") from exc
    _require(hashlib.sha256(raw).hexdigest() == sha, "prerequisite_receipt_mismatch")
    try:
        data = _json(raw)
    except RecognizeRefusal as exc:
        raise RecognizeRefusal("prerequisite_receipt_mismatch") from exc
    bindings, counts = data.get("bindings"), data.get("counts")
    own, other = ("admitted", "posted_total") if kind == "unpaid" else ("posted_total", "admitted")
    ok = (
        data.get("mode") == "apply"
        and isinstance(bindings, dict)
        and isinstance(counts, dict)
        and own in data
        and other not in data
        and counts.get("refused") == 0
        and bindings.get("tenant_id") == str(tenant)
        and bindings.get("snapshot_sha256") == manifest.snapshot_sha256
        and bindings.get("candidate_revision", manifest.candidate_revision) == manifest.candidate_revision
    )
    _require(ok, "prerequisite_receipt_mismatch")
    return sha


def verify_prerequisites(args, tenant: UUID, manifest) -> dict:
    """Digests to bind into the plan. Plan mode may run without them (None; the receipt says they will be
    required); apply refuses unless both verify."""
    result = dict.fromkeys(PREREQUISITES)
    for key, path, sha, kind in (
        ("unpaid_receipt_sha256", args.unpaid_receipt, args.unpaid_receipt_sha256, "unpaid"),
        ("b1b_receipt_sha256", args.b1b_receipt, args.b1b_receipt_sha256, "b1b"),
    ):
        if path is None and sha is None and not args.apply:
            continue
        result[key] = _prerequisite(path, sha, kind=kind, tenant=tenant, manifest=manifest)
    return result


def _legacy_reason(exc: ValueError) -> str | None:
    reason = getattr(exc, "reason", None)
    return reason if isinstance(reason, str) and _REASON.fullmatch(reason) else None


def _facts(evidence) -> dict:
    """Amounts of one verified invoice, as strings. The private plan only; counts and totals leave in the receipt."""
    return dict(
        total=_money(evidence.total_amount),
        settled=_money(evidence.settled_amount),
        withholding=_money(evidence.withholding_tax_amount),
        residual=_money(evidence.residual_amount),
        settlements=len(evidence.settlements),
    )


def _classify(session: Session, scope, verifier, invoice_id: UUID, referenced: frozenset[str]):
    """-> (state, evidence). state None = eligible; 'already_done' = admitted under this very evidence."""
    from dotmac_payables.contracts import InvoiceStatus
    from dotmac_payables.models import (
        AccountingReceipt,
        HistoricalInvoiceReceipt,
        HistoricalSettlementRecord,
        InvoiceRecognition,
        LiabilityEvent,
        PaymentObligation,
        SupplierInvoice,
    )

    from erp_v2.importer.payables_history import HistoricalEvidenceError

    tenant = scope.tenant_id
    invoice = session.scalar(
        sa.select(SupplierInvoice).where(SupplierInvoice.tenant_id == tenant, SupplierInvoice.id == invoice_id)
    )
    if invoice is None:
        return "invoice_not_found", None
    if str(invoice_id) in referenced:
        # Re-entered as a NEW ERP invoice with this legacy reference: never also admitted.
        return "legacy_reference_entered", None

    def exists(model, *conditions) -> bool:
        return session.scalar(sa.select(model.id).where(model.tenant_id == tenant, *conditions).limit(1)) is not None

    try:
        evidence = verifier.evidence(scope=scope, invoice_id=invoice_id, destination=session)
    except HistoricalEvidenceError as exc:
        reason = exc.reason if _REASON.fullmatch(exc.reason) else "evidence_refused"
        return "legacy_evidence:" + reason, None
    receipt = session.scalar(
        sa.select(HistoricalInvoiceReceipt).where(
            HistoricalInvoiceReceipt.tenant_id == tenant, HistoricalInvoiceReceipt.invoice_id == invoice_id
        )
    )
    if receipt is not None:
        if receipt.evidence_fingerprint == verifier.command(evidence).evidence_fingerprint:
            return "already_done", evidence
        return "historical_receipt_other_evidence", None
    if (
        exists(InvoiceRecognition, InvoiceRecognition.invoice_id == invoice_id)
        or exists(PaymentObligation, PaymentObligation.invoice_id == invoice_id)
        or exists(
            LiabilityEvent, LiabilityEvent.document_kind == DOCUMENT_KIND, LiabilityEvent.document_id == invoice_id
        )
        or exists(
            AccountingReceipt,
            AccountingReceipt.document_kind == DOCUMENT_KIND,
            AccountingReceipt.document_id == invoice_id,
        )
    ):
        return "existing_ledger_state", None
    if invoice.status != InvoiceStatus.APPROVED:
        return "not_approved", None
    taken = [item.source_settlement_id for item in evidence.settlements]
    if exists(HistoricalSettlementRecord, HistoricalSettlementRecord.source_settlement_id.in_(taken)):
        return "settlement_already_admitted", None
    return None, evidence


def build_plan(
    session: Session, scope, verifier, ids, *, bindings: dict, referenced: frozenset[str] = frozenset()
) -> dict:
    """Read-only. Returns the plan (with private ids for apply) and its digest."""
    eligible, done, refused, evidence_of = [], [], [], {}
    for invoice_id in ids:
        state, evidence = _classify(session, scope, verifier, invoice_id, referenced)
        if state == "already_done":
            done.append(dict(invoice_id=str(invoice_id), **_facts(evidence)))
        elif state is not None:
            refused.append(dict(invoice_id=str(invoice_id), reason=state))
        else:
            command = verifier.command(evidence)
            notes = admission_notes(
                verifier.source, invoice_id, verifier.review(scope=scope, invoice_id=invoice_id, destination=session)
            )
            eligible.append(
                dict(
                    invoice_id=str(invoice_id),
                    evidence_fingerprint=command.evidence_fingerprint,
                    **({"admission": notes} if notes else {}),
                    **_facts(evidence),
                )
            )
            evidence_of[str(invoice_id)] = [item.source_settlement_id for item in evidence.settlements]
    # A legacy allocation settles exactly one invoice (A5-2): any id presented twice refuses every holder.
    seen = Counter(sid for sids in evidence_of.values() for sid in sids)
    clash = {inv for inv, sids in evidence_of.items() if any(seen[s] > 1 for s in sids)}
    if clash:
        refused.extend(dict(invoice_id=inv, reason="settlement_id_duplicate") for inv in sorted(clash))
        eligible = [row for row in eligible if row["invoice_id"] not in clash]
    detail = (
        [dict(outcome="eligible", reason=None, **row) for row in eligible]
        + [dict(outcome="already_done", reason=None, **row) for row in done]
        + [dict(outcome="refused", **row) for row in refused]
    )
    detail.sort(key=lambda row: row["invoice_id"])
    plan = dict(version=1, bindings=bindings, eligible=eligible, already_done=done, refused=refused, detail=detail)
    plan["digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return plan


def plan_file_of(plan: dict) -> dict:
    """The private per-invoice plan: ids, amounts and reason codes. Never goes in the receipt."""
    return dict(version=1, plan_digest=plan["digest"], bindings=plan["bindings"], invoices=plan["detail"])


def _totals(rows: list[dict]) -> dict:
    keys = ("total", "settled", "withholding", "residual")
    result = {key: _money(sum((Decimal(row[key]) for row in rows), Decimal(0))) for key in keys}
    result["settlements"] = sum(row["settlements"] for row in rows)
    return result


def receipt_of(plan: dict, *, mode: str, applied: int = 0) -> dict:
    """Counts and totals only: never invoice, payment or settlement ids, names or other personal data."""
    payload = dict(
        mode=mode,
        production_ready=False,
        bindings=plan["bindings"],
        plan_digest=plan["digest"],
        counts=dict(
            requested=len(plan["eligible"]) + len(plan["already_done"]) + len(plan["refused"]),
            eligible=len(plan["eligible"]),
            already_done=len(plan["already_done"]),
            refused=len(plan["refused"]),
        ),
        refusals_by_reason=dict(sorted(Counter(item["reason"] for item in plan["refused"]).items())),
        totals=dict(eligible=_totals(plan["eligible"]), already_done=_totals(plan["already_done"])),
        reviewed_admissions=reviewed_admissions(plan["eligible"]),
    )
    if mode == "plan" and not all(plan["bindings"].get(key) for key in PREREQUISITES):
        payload["prerequisites_required_at_apply"] = list(PREREQUISITES)
    if mode == "apply":
        payload["applied"] = applied
    return payload


def _assert_landed(session: Session, tenant: UUID, row: dict) -> None:
    """Amounts must never change: the owner's rows must equal the reviewed plan, exactly."""
    from dotmac_payables.models import HistoricalSettlementRecord, PaymentObligation

    invoice_id = UUID(row["invoice_id"])
    obligation = session.scalar(
        sa.select(PaymentObligation).where(
            PaymentObligation.tenant_id == tenant, PaymentObligation.invoice_id == invoice_id
        )
    )
    _require(obligation is not None, "apply_obligation_missing")
    rows = list(
        session.scalars(
            sa.select(HistoricalSettlementRecord).where(
                HistoricalSettlementRecord.tenant_id == tenant,
                HistoricalSettlementRecord.obligation_id == obligation.id,
            )
        )
    )
    _require(
        Decimal(obligation.original_amount) == Decimal(row["total"])
        and Decimal(obligation.outstanding_amount) == Decimal(row["residual"])
        and len(rows) == row["settlements"]
        and sum((Decimal(r.applied_amount) for r in rows), Decimal(0)) == Decimal(row["settled"])
        and sum((Decimal(r.withholding_amount) for r in rows), Decimal(0)) == Decimal(row["withholding"]),
        "apply_amounts_changed",
    )


def reviewed_admissions(rows: list[dict]) -> dict:
    """Counts of Finance-reviewed admissions in a plan: by waived check, and those that still need a correcting
    journal (``requires_correcting_journal``: B-9 cannot close the exception without it)."""
    notes = [row["admission"] for row in rows if row.get("admission")]
    return dict(
        waived=dict(sorted(Counter(name for note in notes for name in note["waived_checks"]).items())),
        requires_correcting_journal=sum(1 for note in notes if note["requires_correcting_journal"]),
    )


def apply_plan(
    session: Session, scope, verifier, plan: dict, *, reviewed_digest: str, actor: str, list_sha256: str, now: datetime
) -> int:
    """Admit every eligible invoice in the caller's transaction (never commits). Returns the count."""
    from dotmac_kernel.audit import write_audit_event
    from dotmac_kernel.idempotency import IdempotencyConflict
    from dotmac_payables import admit_historical_settled_invoice
    from dotmac_payables.contracts import PayablesError

    from erp_v2.legacy_ap.audit import AUDIT_SETTLED_ADMITTED, SOURCE_APPLICATION

    _require(plan["digest"] == reviewed_digest, "reviewed_plan_changed")
    _require(not plan["refused"], "list_not_fully_eligible")
    tenant = scope.tenant_id
    for row in plan["eligible"]:
        invoice_id = UUID(row["invoice_id"])
        try:
            with session.begin_nested():
                evidence = verifier.evidence(scope=scope, invoice_id=invoice_id, destination=session)
                command = verifier.command(evidence)
                _require(command.evidence_fingerprint == row["evidence_fingerprint"], "evidence_changed")
                admit_historical_settled_invoice(
                    session,
                    scope=scope,
                    command=command,
                    verifier=verifier,
                    idempotency_key=_identity_key(invoice_id, command.source_snapshot_sha256),
                    idempotency_expires_at=None,
                    admitted_at=now,
                )
                _assert_landed(session, tenant, row)
                write_audit_event(
                    session,
                    tenant_id=tenant,
                    actor_party_id=UUID(actor),
                    actor_type="user",
                    actor_id=actor,
                    source_application=SOURCE_APPLICATION,
                    action=AUDIT_SETTLED_ADMITTED,
                    entity_type="supplier_invoice",
                    entity_id=str(invoice_id),
                    is_success=True,
                    details={
                        "list_sha256": list_sha256,
                        "evidence_fingerprint": command.evidence_fingerprint,
                        "settlements": row["settlements"],
                        **admission_notes(
                            verifier.source,
                            invoice_id,
                            verifier.review(scope=scope, invoice_id=invoice_id, destination=session),
                        ),
                    },
                )
        except RecognizeRefusal:
            raise
        except (PayablesError, IdempotencyConflict) as exc:
            raise RecognizeRefusal("apply_payables_refused") from exc
        except ValueError as exc:
            reason = _legacy_reason(exc)
            raise RecognizeRefusal("apply_legacy_evidence:" + reason if reason else "apply_refused") from exc
    return len(plan["eligible"])


def run_cohort(
    session: Session,
    source: sa.Connection,
    manifest,
    *,
    tenant: UUID,
    ids,
    list_sha256: str,
    actor_party_id: UUID,
    apply: bool,
    reviewed_digest: str | None = None,
    bindings: dict | None = None,
    now: datetime | None = None,
    plan_sink=None,
    referenced: frozenset[str] = frozenset(),
) -> dict:
    """Core of both modes; caller owns the transaction and (for plan) its READ ONLY setting."""
    from dotmac_kernel.cache import TenantScope
    from dotmac_kernel.models import Party, PartyRoleGrant, Role
    from dotmac_payables.models import HistoricalInvoiceReceipt, HistoricalSettlementRecord

    from erp_v2.legacy_ap.settled_verifier import SettledInvoiceVerifier

    scope = TenantScope(tenant)
    if apply:
        # Run order is enforced, not documented: the unpaid import and B1b receipts are bound into the
        # reviewed plan (``bindings``), so the digest an operator reviews names them.
        _require(
            all(re.fullmatch(r"[0-9a-f]{64}", str((bindings or {}).get(key) or "")) for key in PREREQUISITES),
            "prerequisite_receipt_missing",
        )
    party = session.scalar(sa.select(Party).where(Party.tenant_id == tenant, Party.id == actor_party_id))
    _require(party is not None and party.is_active, "approver_unknown")
    held = session.scalar(
        sa.select(PartyRoleGrant.id)
        .join(Role, sa.and_(Role.tenant_id == PartyRoleGrant.tenant_id, Role.id == PartyRoleGrant.role_id))
        .where(
            PartyRoleGrant.tenant_id == tenant,
            PartyRoleGrant.party_id == actor_party_id,
            Role.slug == FINANCE_APPROVER,
        )
        .limit(1)
    )
    _require(held is not None, "approver_lacks_finance_approver")
    verifier = SettledInvoiceVerifier(source, manifest)
    if apply:
        # One settled apply at a time per TENANT (not per list: overlapping lists must serialize), taken
        # BEFORE anything is read: a concurrent apply waits here and then plans from the committed state.
        session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "erp.settled:" + str(tenant)}
        )
    # The preflight's empty-table rule does not cover historical_settlements for this command: rows may
    # exist only for invoices of THIS list (a re-run). Anything else is another admission's history.
    outside = session.scalar(
        sa.select(sa.func.count())
        .select_from(HistoricalSettlementRecord)
        .join(
            HistoricalInvoiceReceipt,
            sa.and_(
                HistoricalInvoiceReceipt.tenant_id == HistoricalSettlementRecord.tenant_id,
                HistoricalInvoiceReceipt.id == HistoricalSettlementRecord.receipt_id,
            ),
        )
        .where(HistoricalSettlementRecord.tenant_id == tenant, HistoricalInvoiceReceipt.invoice_id.not_in(list(ids)))
    )
    _require(outside == 0, "destination_settlements_outside_list")
    plan = build_plan(session, scope, verifier, ids, bindings=bindings or {}, referenced=referenced)
    if plan_sink is not None:
        plan_sink(plan_file_of(plan))
    if not apply:
        return receipt_of(plan, mode="plan")
    _require(bool(re.fullmatch(r"[0-9a-f]{64}", reviewed_digest or "")), "reviewed_plan_required")
    applied = apply_plan(
        session,
        scope,
        verifier,
        plan,
        reviewed_digest=reviewed_digest,
        actor=str(actor_party_id),
        list_sha256=list_sha256,
        now=now or datetime.now(UTC),
    )
    return receipt_of(plan, mode="apply", applied=applied)


def _preflight(session: Session, tenant: UUID) -> None:
    """The inherited destination preflight (non-privileged role, exact heads, tenant binding), with the
    empty-table rule narrowed to everything except ``historical_settlements`` (see module docstring)."""
    from erp_v2.importer import payables_history_cli

    try:
        payables_history_cli._destination_preflight(
            session, tenant, payables_history_cli.SETTLED_PREIMPORT_EMPTY_TABLES
        )
    except payables_history_cli.OperatorRefusal as exc:
        raise RecognizeRefusal(exc.reason) from exc


def _run(args) -> dict:
    from importlib.metadata import version

    from erp_v2.importer.identity_cli import _manifest, _safe_engine, _sha
    from erp_v2.importer.payables_history_cli import REFERENCED_SQL

    source_url = os.environ.pop("LEGACY_DATABASE_URL", "")
    destination_url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    _require(_authorized(args), "explicit_apply_authorization_required")
    _require(bool(source_url) and bool(destination_url), "operator_database_materials_required")
    _require(
        "DATABASE_URL" not in os.environ and "PLATFORM_DATABASE_URL" not in os.environ,
        "serving_database_materials_forbidden",
    )
    _install_audit_actions()
    manifest_bytes, list_bytes = args.manifest.read_bytes(), args.list.read_bytes()
    try:
        manifest = _manifest(json.dumps(_json(manifest_bytes)).encode())
    except ValueError as exc:
        raise RecognizeRefusal("invalid_manifest") from exc
    _require(
        isinstance(manifest.candidate_revision, str)
        and bool(re.fullmatch(r"[0-9a-f]{40}", manifest.candidate_revision)),
        "full_candidate_revision_required",
    )
    snapshot_sha = _sha(args.snapshot_file)
    _require(snapshot_sha == manifest.snapshot_sha256, "snapshot_digest_mismatch")
    tenant, _census_ref, ids = parse_list(list_bytes, args.list_sha256, manifest)
    actor = _uuid(args.approver_party_id, "invalid_approver_party_id")
    if args.apply:
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", args.reviewed_plan_sha256 or "")), "reviewed_plan_required")
    _require(version("dotmac-payables") == REQUIRED_PAYABLES_VERSION, "released_payables_version_required")
    prerequisites = verify_prerequisites(args, tenant, manifest)
    bindings = dict(
        **prerequisites,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        list_sha256=args.list_sha256,
        snapshot_sha256=snapshot_sha,
        candidate_revision=manifest.candidate_revision,
        tenant_id=str(tenant),
        approver_party_id=str(actor),
        source_owner=SOURCE_OWNER,
    )
    source_engine = _safe_engine(source_url)
    try:
        destination_engine = _safe_engine(destination_url)
        try:
            with source_engine.connect() as source, source.begin():
                source.execute(sa.text("SET TRANSACTION READ ONLY"))
                source.execute(sa.text("SET LOCAL row_security = off"))
                _require(
                    source.scalar(sa.text("SELECT current_database()")) == manifest.source_database,
                    "source_database_mismatch",
                )
                with Session(destination_engine) as session, session.begin():
                    if not args.apply:
                        session.execute(sa.text("SET TRANSACTION READ ONLY"))
                    _require(
                        session.scalar(sa.text("SELECT current_database()")) == manifest.destination_database,
                        "destination_database_mismatch",
                    )
                    _preflight(session, tenant)
                    referenced = frozenset(
                        str(value) for value in session.scalars(sa.text(REFERENCED_SQL).bindparams(tenant=tenant))
                    )
                    return run_cohort(
                        session,
                        source,
                        manifest,
                        tenant=tenant,
                        ids=ids,
                        list_sha256=args.list_sha256,
                        actor_party_id=actor,
                        apply=args.apply,
                        reviewed_digest=args.reviewed_plan_sha256,
                        bindings=bindings,
                        plan_sink=lambda payload: _dump(args.plan_fd, payload),
                        referenced=referenced,
                    )
        finally:
            destination_engine.dispose()
    finally:
        source_engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.legacy_ap.settled_cli")
    for name in ("manifest", "snapshot-file", "list", "receipt", "plan-file"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--list-sha256", required=True)
    parser.add_argument("--approver-party-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed-plan-sha256")
    # Apply receipts of the two earlier steps (required for --apply; bound into the reviewed plan).
    parser.add_argument("--unpaid-receipt", type=Path)
    parser.add_argument("--unpaid-receipt-sha256")
    parser.add_argument("--b1b-receipt", type=Path)
    parser.add_argument("--b1b-receipt-sha256")
    args = parser.parse_args(argv)
    try:
        _require(_authorized(args), "explicit_apply_authorization_required")
        fd = _receipt(args.receipt)
        args.plan_fd = _receipt(args.plan_file)  # private per-invoice plan: 0600, O_EXCL, no symlinks
    except Exception:
        print("legacy settled admission refused: authorization or private receipt path")
        return 2
    with os.fdopen(fd, "w") as stream, os.fdopen(args.plan_fd, "a"):
        try:
            payload = _run(args)
            code = 0 if payload["counts"]["refused"] == 0 else 3
        except RecognizeRefusal as exc:
            payload, code = dict(production_ready=False, refusal=exc.reason), 2
        except Exception:
            payload, code = dict(production_ready=False, refusal="settled_admission_command_refused"), 2
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    counts = payload.get("counts", {})
    print(
        "legacy settled admission: "
        + (
            f"requested={counts.get('requested', 0)} eligible={counts.get('eligible', 0)} "
            f"already_done={counts.get('already_done', 0)} refused={counts.get('refused', 0)}"
            if counts
            else "command refused"
        )
        + "; production_ready=false"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
