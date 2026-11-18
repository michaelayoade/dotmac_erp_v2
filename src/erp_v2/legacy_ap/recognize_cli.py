"""Operator CLI (B1b): recognize and post legacy invoices that were APPROVED but never posted.

Michael's ruling: "approve and post, this is the proper accounting settlement". The importer
leaves such invoices APPROVED by direct update with no liability event, obligation or journal,
so B1 (``approve_and_post``) cannot run on them. Per invoice, in ONE savepoint of ONE operator
transaction:

    payables.recognize_approved_invoice   -> invoice_recognized event + obligations from the
                                              invoice's own schedule (verifier-proved never posted)
    payables.build_invoice_accounting_consequence
    erp_v2.posting.post_consequence        -> Dr expense (+ Dr tax), Cr liability account (2000)
    payables.record_accounting_receipt

Amounts are never changed: the consequence is the owner's, and each journal is asserted to
carry exactly the invoice total on both sides before the next invoice starts.

Input list (JSON, produced from the step-3 census; this command only validates it), pinned by
``--list-sha256``::

    {"version": 1, "tenant_id": "<uuid>", "census_ref": "<text>",
     "snapshot_sha256": "<64 hex>", "candidate_revision": "<40 hex>",
     "invoice_ids": ["<canonical uuid>", ...]}

Invoice ids are the ERP invoice ids, which equal the legacy invoice ids. The list is bound to the
snapshot manifest (digest, candidate revision, single tenant). Evidence that legacy never posted
each invoice is read read-only from the restored legacy snapshot (``ApprovedInvoiceVerifier``).

Plan mode (default) runs in a READ ONLY transaction and writes nothing. Apply needs ``--apply``,
``--reviewed-plan-sha256`` (the plan digest an operator reviewed; recomputed and compared), and
ERP_IMPORT_APPROVED=1 + ERP_IMPORT_APPLY_OK=1. Apply refuses unless every id is eligible or already
done. Re-running a completed list is a no-op. Refused: not APPROVED, already recognized under other
evidence, historical receipt, existing ledger state, dimensions, unresolvable accounts, closed period,
or any legacy-evidence failure. The receipt carries counts, reasons-by-count and money totals plus
digests only: no invoice ids, names or other personal data. The legacy and migration DSNs are read from
LEGACY_DATABASE_URL and MIGRATION_DATABASE_URL and removed from the environment before use.
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

REQUIRED_PAYABLES_VERSION = "0.1.0a5"
MAX_INVOICES = 1000
DOCUMENT_KIND = "supplier_invoice"
FINANCE_APPROVER = "finance_approver"
_REASON = re.compile(r"[a-z][a-z0-9_:.-]*")


class RecognizeRefusal(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise RecognizeRefusal(reason)


def _json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, RecognizeRefusal):
            raise
        raise RecognizeRefusal("invalid_json_document") from exc
    _require(isinstance(value, dict), "json_object_required")
    return value


def _uuid(value, reason: str) -> UUID:
    try:
        ident = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RecognizeRefusal(reason) from exc
    _require(str(ident) == value, reason)
    return ident


def parse_list(raw: bytes, expected_sha256: str, manifest) -> tuple[UUID, str, tuple[UUID, ...]]:
    """Validate the sha256-pinned census list against the snapshot manifest."""
    _require(bool(re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "")), "list_sha256_required")
    _require(hashlib.sha256(raw).hexdigest() == expected_sha256, "list_digest_mismatch")
    data = _json(raw)
    _require(
        set(data) == {"version", "tenant_id", "census_ref", "snapshot_sha256", "candidate_revision", "invoice_ids"},
        "invalid_list_schema",
    )
    _require(type(data["version"]) is int and data["version"] == 1, "invalid_list_version")
    _require(data["snapshot_sha256"] == manifest.snapshot_sha256, "list_snapshot_mismatch")
    _require(data["candidate_revision"] == manifest.candidate_revision, "list_candidate_mismatch")
    tenant = _uuid(data["tenant_id"], "invalid_tenant_id")
    _require(tuple(manifest.tenant_extent) == (str(tenant),), "tenant_extent_mismatch")
    ref = data["census_ref"]
    _require(
        isinstance(ref, str) and bool(ref.strip()) and ref == ref.strip() and len(ref) <= 120, "census_ref_required"
    )
    rows = data["invoice_ids"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= MAX_INVOICES, "explicit_bounded_list_required")
    ids = tuple(_uuid(value, "invalid_invoice_id") for value in rows)
    _require(len(set(ids)) == len(ids), "duplicate_invoice_id")
    return tenant, ref, tuple(sorted(ids, key=str))


def evidence_reference(list_sha256: str) -> str:
    return f"erp.b1b.list:{list_sha256}"


def approval_reference(list_sha256: str, actor: str) -> str:
    return f"erp.b1b:recognize:{list_sha256}:approver:{actor}"


def _money(value) -> str:
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def _classify(
    session: Session, tenant: UUID, invoice_id: UUID, evidence_ref: str, actor: UUID, review=None
) -> str | None:
    """Destination-side state. None = eligible; 'already_done' = completed under this list.

    ``review`` is the Finance decision row (reviewed admission), if any. ``ias8_post`` (D-M5) replaces the
    "invoice period is open" rule by the IAS 8 rule: the invoice's period is closed to posting and a later
    OPEN period exists; the journal posts there, expense side on 3100 (``erp_v2.legacy_ap.ias8``)."""
    from dotmac_accounting.contracts import PeriodStatus
    from dotmac_accounting.models import FiscalPeriod
    from dotmac_payables.contracts import InvoiceStatus
    from dotmac_payables.models import (
        AccountingReceipt,
        HistoricalInvoiceReceipt,
        InvoiceRecognition,
        LiabilityEvent,
        PaymentObligation,
        SupplierInvoice,
        SupplierInvoiceLine,
    )

    from erp_v2.governance.policy import dual_control
    from erp_v2.legacy_ap import ias8 as ias8_mode
    from erp_v2.posting import bridge, rules
    from erp_v2.posting.rules import PostingRefused
    from erp_v2.web.revenue_approval_services import ApprovalRefused, _submitter_party_id

    invoice = session.scalar(
        sa.select(SupplierInvoice).where(SupplierInvoice.tenant_id == tenant, SupplierInvoice.id == invoice_id)
    )
    if invoice is None:
        return "invoice_not_found"

    def exists(model, *conditions) -> bool:
        return session.scalar(sa.select(model.id).where(model.tenant_id == tenant, *conditions).limit(1)) is not None

    if exists(HistoricalInvoiceReceipt, HistoricalInvoiceReceipt.invoice_id == invoice_id):
        return "historical_receipt"
    receipt = exists(
        AccountingReceipt, AccountingReceipt.document_kind == DOCUMENT_KIND, AccountingReceipt.document_id == invoice_id
    )
    recognition = session.scalar(
        sa.select(InvoiceRecognition).where(
            InvoiceRecognition.tenant_id == tenant, InvoiceRecognition.invoice_id == invoice_id
        )
    )
    if recognition is not None:
        return "already_done" if recognition.evidence_reference == evidence_ref and receipt else "already_recognized"
    if invoice.status != InvoiceStatus.APPROVED:
        return "not_approved"
    if (
        receipt
        or exists(
            LiabilityEvent, LiabilityEvent.document_kind == DOCUMENT_KIND, LiabilityEvent.document_id == invoice_id
        )
        or exists(PaymentObligation, PaymentObligation.invoice_id == invoice_id)
    ):
        return "existing_ledger_state"
    # Separation of duties, resolved the B1-1 way: imported submitted_by/approved_by hold legacy
    # credential ids, native ones party ids. The approver must be neither the submitter nor the
    # legacy approver; an unrecorded or unresolvable person cannot be proven different, so it refuses.
    # Only when the tenant's dual-control policy is on (default off: no compulsory two-person).
    for recorded, label in (
        ((invoice.submitted_by, "submitter"), (invoice.approved_by, "legacy_approver"))
        if dual_control(session, tenant)
        else ()
    ):
        if not recorded:
            return f"{label}_unknown"
        try:
            person = _submitter_party_id(session, tenant, recorded)
        except ApprovalRefused:
            return f"{label}_unresolved"
        if person == actor:
            return f"approver_is_{label}"
    lines = list(
        session.scalars(
            sa.select(SupplierInvoiceLine).where(
                SupplierInvoiceLine.tenant_id == tenant, SupplierInvoiceLine.invoice_id == invoice_id
            )
        )
    )
    if any(line.dimension_refs for line in lines):
        return "dimensions_unsupported"
    # Payables a5 posts at posting_date = invoice_date (Michael: original invoice date, into its
    # original period; the date is never changed). The containing period must exist, be OPEN and not
    # be an adjustment period. The accounting owner re-checks this when posting (backstop).
    ias8 = review is not None and review.decision == "ias8_post"
    posting_date = invoice.invoice_date
    expense_ref = None
    if ias8:
        try:
            posting_date = ias8_mode.posting_date_for(session, tenant, invoice.invoice_date)
            expense_ref = ias8_mode.require_account(session, tenant, review.ias8_account_id)
        except ias8_mode.Ias8Refused as exc:
            return exc.reason
    else:
        periods = list(
            session.scalars(
                sa.select(FiscalPeriod).where(
                    FiscalPeriod.tenant_id == tenant,
                    FiscalPeriod.start_date <= invoice.invoice_date,
                    FiscalPeriod.end_date >= invoice.invoice_date,
                )
            )
        )
        if not periods or any(p.status != PeriodStatus.OPEN or p.is_adjustment for p in periods):
            return "posting_period_not_open"
    refs = [invoice.liability_account_ref]
    for line in lines:
        refs.append(expense_ref or line.posting_account_ref)
        if line.tax_amount > 0:
            if not line.tax_account_ref:
                return "tax_account_missing"
            refs.append(line.tax_account_ref)
    try:
        bridge._resolve_accounts(
            session, tenant, rules.policy_for("payables", "supplier_invoice"), refs, require_open=True
        )
        bridge._require_open_period(session, tenant, posting_date)
    except PostingRefused as exc:
        return f"posting_{exc.code}"
    return None


def build_plan(session: Session, scope, verifier, ids, *, evidence_ref: str, bindings: dict, actor: UUID) -> dict:
    """Read-only. Returns the plan (with private ids for apply) and its digest."""
    from erp_v2.importer.payables_history import HistoricalEvidenceError

    tenant = scope.tenant_id
    eligible, done, refused = [], [], []
    for invoice_id in ids:
        try:
            review = verifier.review(scope=scope, invoice_id=invoice_id, destination=session)
        except HistoricalEvidenceError as exc:
            refused.append(dict(invoice_id=str(invoice_id), reason="legacy_evidence:" + exc.reason))
            continue
        state = _classify(session, tenant, invoice_id, evidence_ref, actor, review)
        if state == "already_done":
            done.append(dict(invoice_id=str(invoice_id)))
            continue
        if state is not None:
            refused.append(dict(invoice_id=str(invoice_id), reason=state))
            continue
        try:
            evidence = verifier.evidence(scope=scope, invoice_id=invoice_id, destination=session)
        except HistoricalEvidenceError as exc:
            reason = exc.reason if _REASON.fullmatch(exc.reason) else "evidence_refused"
            refused.append(dict(invoice_id=str(invoice_id), reason="legacy_evidence:" + reason))
            continue
        invoice = _invoice_amounts(session, tenant, invoice_id)
        extra = {}
        if review is not None and review.decision == "ias8_post":
            extra["ias8"] = dict(review_id=review.review_id, decision_file_sha=review.decision_file_sha)
        elif review is not None and review.waived:
            extra["waived"] = list(review.waived)
        eligible.append(
            dict(invoice_id=str(invoice_id), evidence_fingerprint=evidence.fingerprint(), **invoice, **extra)
        )
    # Per-invoice detail for the PRIVATE plan file only (never the receipt): id, date, amounts, outcome.
    detail = []
    for outcome, items in (("eligible", eligible), ("already_done", done), ("refused", refused)):
        for item in items:
            facts = _invoice_amounts(session, tenant, UUID(item["invoice_id"]))
            detail.append(dict(invoice_id=item["invoice_id"], outcome=outcome, reason=item.get("reason"), **facts))
    detail.sort(key=lambda row: row["invoice_id"])
    plan = dict(
        version=1,
        bindings=bindings,
        eligible=eligible,
        already_done=done,
        refused=refused,
        detail=detail,
    )
    plan["digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return plan


def _invoice_amounts(session: Session, tenant: UUID, invoice_id: UUID) -> dict:
    """Date and amounts of an invoice as stored (empty if it does not exist). No names, no PII."""
    from dotmac_accounting.models import FiscalPeriod as FiscalPeriod_
    from dotmac_payables.models import SupplierInvoice

    invoice = session.scalar(
        sa.select(SupplierInvoice).where(SupplierInvoice.tenant_id == tenant, SupplierInvoice.id == invoice_id)
    )
    if invoice is None:
        return {}
    periods = sorted(
        {
            f"{p.start_date.isoformat()}..{p.end_date.isoformat()}"
            for p in session.scalars(
                sa.select(FiscalPeriod_).where(
                    FiscalPeriod_.tenant_id == tenant,
                    FiscalPeriod_.start_date <= invoice.invoice_date,
                    FiscalPeriod_.end_date >= invoice.invoice_date,
                )
            )
        }
    )
    return dict(
        period=",".join(periods) or "none",
        invoice_date=invoice.invoice_date.isoformat(),
        subtotal=_money(invoice.subtotal),
        tax=_money(invoice.tax_amount),
        total=_money(invoice.total_amount),
    )


def _totals(rows: list[dict]) -> dict:
    return {key: _money(sum((Decimal(row[key]) for row in rows), Decimal(0))) for key in ("subtotal", "tax", "total")}


def plan_file_of(plan: dict) -> dict:
    """The private per-invoice plan: ids, dates, amounts and reason codes. Never goes in the receipt."""
    return dict(version=1, plan_digest=plan["digest"], bindings=plan["bindings"], invoices=plan["detail"])


def _by_period(rows: list[dict]) -> dict:
    """Eligible invoices by the accounting period they will post into: count and total only."""
    result: dict[str, dict] = {}
    for row in rows:
        entry = result.setdefault(row["period"], dict(count=0, total=Decimal(0)))
        entry["count"] += 1
        entry["total"] += Decimal(row["total"])
    return {key: dict(count=v["count"], total=_money(v["total"])) for key, v in sorted(result.items())}


def receipt_of(plan: dict, *, mode: str, applied: int = 0, posted_total: Decimal | None = None) -> dict:
    """Counts and totals only: never invoice ids, names or other personal data."""
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
        totals=dict(eligible=_totals(plan["eligible"])),
        by_period=_by_period(plan["eligible"]),
        ias8_prior_period=sum(1 for item in plan["eligible"] if item.get("ias8")),
        waived=dict(sorted(Counter(name for item in plan["eligible"] for name in item.get("waived", ())).items())),
    )
    if mode == "apply":
        payload["applied"] = applied
        payload["posted_total"] = _money(posted_total if posted_total is not None else 0)
    return payload


def apply_plan(
    session: Session,
    scope,
    verifier,
    plan: dict,
    *,
    reviewed_digest: str,
    actor: str,
    list_sha256: str,
    now: datetime,
) -> Decimal:
    """Recognize and post every eligible invoice in the caller's transaction (never commits)."""
    from dotmac_kernel.audit import write_audit_event
    from dotmac_kernel.idempotency import IdempotencyConflict
    from dotmac_payables import service as payables
    from dotmac_payables.contracts import AccountingReceiptInput, PayablesError
    from dotmac_payables.models import SupplierInvoice, SupplierInvoiceLine

    from erp_v2.importer.payables_history import admission_notes
    from erp_v2.legacy_ap import ias8 as ias8_mode
    from erp_v2.legacy_ap.audit import AUDIT_RECOGNIZED_POSTED, SOURCE_APPLICATION
    from erp_v2.posting.bridge import post_consequence
    from erp_v2.posting.rules import PostingRefused

    _require(plan["digest"] == reviewed_digest, "reviewed_plan_changed")
    _require(not plan["refused"], "list_not_fully_eligible")
    tenant = scope.tenant_id
    approval_ref = approval_reference(list_sha256, actor)
    posted_total = Decimal(0)
    for row in plan["eligible"]:
        invoice_id = UUID(row["invoice_id"])
        try:
            with session.begin_nested():
                evidence = verifier.evidence(scope=scope, invoice_id=invoice_id, destination=session)
                command = verifier.command(evidence, recognized_by=actor, approval_reference=approval_ref)
                _require(command.evidence_fingerprint == row["evidence_fingerprint"], "evidence_changed")
                payables.recognize_approved_invoice(
                    session,
                    scope=scope,
                    command=command,
                    verifier=verifier,
                    idempotency_key=f"erp.b1b.recognize:{invoice_id}",
                    idempotency_expires_at=None,
                    recognized_at=now,
                )
                invoice = session.scalar(
                    sa.select(SupplierInvoice).where(
                        SupplierInvoice.tenant_id == tenant, SupplierInvoice.id == invoice_id
                    )
                )
                consequence = payables.build_invoice_accounting_consequence(
                    session, tenant_id=tenant, invoice_id=invoice_id
                )
                debit = sum((Decimal(e.debit) for e in consequence.entries), Decimal(0))
                credit = sum((Decimal(e.credit) for e in consequence.entries), Decimal(0))
                _require(debit == credit == invoice.total_amount, "consequence_amount_mismatch")
                review = verifier.review(scope=scope, invoice_id=invoice_id, destination=session)
                posting, post_ref = consequence, approval_ref
                if row.get("ias8"):
                    # D-M5: the owner's consequence (and its receipt) stay as they are; the journal posts in the
                    # first open period with the expense side on 3100 and carries the review id.
                    _require(
                        review is not None
                        and review.decision == "ias8_post"
                        and review.review_id == row["ias8"]["review_id"],
                        "ias8_review_changed",
                    )
                    posting_date = ias8_mode.posting_date_for(session, tenant, invoice.invoice_date)
                    account = ias8_mode.require_account(session, tenant, review.ias8_account_id)
                    tax_refs = frozenset(
                        line.tax_account_ref
                        for line in session.scalars(
                            sa.select(SupplierInvoiceLine).where(
                                SupplierInvoiceLine.tenant_id == tenant, SupplierInvoiceLine.invoice_id == invoice_id
                            )
                        )
                        if line.tax_amount > 0
                    )
                    posting = ias8_mode.derive(
                        consequence,
                        tax_refs=tax_refs,
                        account_ref=account,
                        posting_date=posting_date,
                        review_id=review.review_id,
                        file_sha=review.decision_file_sha,
                    )
                    post_ref = f"{approval_ref}:ias8:{review.review_id}"
                else:
                    _require(review is None or review.decision != "ias8_post", "ias8_review_changed")
                posted = post_consequence(
                    session, scope=scope, consequence=posting, approval_ref=post_ref, actor=actor, now=now
                )
                payables.record_accounting_receipt(
                    session,
                    scope=scope,
                    command=AccountingReceiptInput(
                        document_kind=DOCUMENT_KIND,
                        document_id=invoice_id,
                        consequence_fingerprint=consequence.fingerprint,
                        accounting_reference=str(posted.journal_id),
                        accounting_evidence_fingerprint=posted.evidence_fingerprint,
                    ),
                    idempotency_key=f"erp.b1b.receipt:{invoice_id}",
                    idempotency_expires_at=None,
                    recorded_at=now,
                )
                write_audit_event(
                    session,
                    tenant_id=tenant,
                    actor_party_id=UUID(actor),
                    actor_type="user",
                    actor_id=actor,
                    source_application=SOURCE_APPLICATION,
                    action=AUDIT_RECOGNIZED_POSTED,
                    entity_type="supplier_invoice",
                    entity_id=str(invoice_id),
                    is_success=True,
                    details={
                        "journal_id": str(posted.journal_id),
                        "consequence_fingerprint": consequence.fingerprint,
                        "list_sha256": list_sha256,
                        "evidence_fingerprint": command.evidence_fingerprint,
                        **admission_notes(verifier.source, invoice_id, review),
                        **({"ias8_posting_date": posting.posting_date.isoformat()} if row.get("ias8") else {}),
                    },
                )
                posted_total += credit
        except RecognizeRefusal:
            raise
        except PostingRefused as exc:
            raise RecognizeRefusal(f"apply_posting_{exc.code}") from exc
        except (PayablesError, IdempotencyConflict) as exc:
            raise RecognizeRefusal("apply_payables_refused") from exc
        except ValueError as exc:
            reason = getattr(exc, "reason", None)
            raise RecognizeRefusal(
                "apply_legacy_evidence:" + reason
                if isinstance(reason, str) and _REASON.fullmatch(reason)
                else "apply_refused"
            ) from exc
    return posted_total


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
) -> dict:
    """Core of both modes; caller owns the transaction and (for plan) its READ ONLY setting."""
    from dotmac_kernel.cache import TenantScope
    from dotmac_kernel.models import Party

    from erp_v2.legacy_ap.recognition_verifier import ApprovedInvoiceVerifier

    scope = TenantScope(tenant)
    from dotmac_kernel.models import PartyRoleGrant, Role

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
    evidence_ref = evidence_reference(list_sha256)
    verifier = ApprovedInvoiceVerifier(source, manifest, evidence_ref)
    if apply:
        # One apply at a time per list, taken BEFORE the plan is built: a concurrent identical apply
        # waits here, then classifies every invoice as already done (a no-op), instead of planning
        # from a snapshot taken before the first one committed.
        session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "erp.b1b:" + list_sha256}
        )
    plan = build_plan(
        session, scope, verifier, ids, evidence_ref=evidence_ref, bindings=bindings or {}, actor=actor_party_id
    )
    if plan_sink is not None:
        plan_sink(plan_file_of(plan))
    if not apply:
        return receipt_of(plan, mode="plan")
    _require(bool(re.fullmatch(r"[0-9a-f]{64}", reviewed_digest or "")), "reviewed_plan_required")
    posted_total = apply_plan(
        session,
        scope,
        verifier,
        plan,
        reviewed_digest=reviewed_digest,
        actor=str(actor_party_id),
        list_sha256=list_sha256,
        now=now or datetime.now(UTC),
    )
    return receipt_of(plan, mode="apply", applied=len(plan["eligible"]), posted_total=posted_total)


def _authorized(args) -> bool:
    return not args.apply or (
        os.environ.get("ERP_IMPORT_APPROVED") == "1" and os.environ.get("ERP_IMPORT_APPLY_OK") == "1"
    )


def _preflight(session: Session, tenant: UUID) -> None:
    """The historical-payables destination preflight: non-privileged role (inherited roles, session_user and
    owned objects included), exact schema heads, and the tenant binding, on the connection actually used."""
    from erp_v2.importer import payables_history_cli

    try:
        payables_history_cli._destination_preflight(session, tenant)
    except payables_history_cli.OperatorRefusal as exc:
        raise RecognizeRefusal(exc.reason) from exc


def _install_audit_actions() -> None:
    from dotmac_kernel.audit_actions import (
        AuditActionRegistry,
        AuditActionsNotInstalledError,
        active_audit_actions,
        install_audit_actions,
    )

    try:
        active_audit_actions()
    except AuditActionsNotInstalledError:
        from erp_v2.assembly import assembly
        from erp_v2.legacy_ap.audit import legacy_recognize_feature

        # This operator command declares its own audit vocabulary; it is not an assembled feature.
        install_audit_actions(AuditActionRegistry.from_manifests([*assembly.modules, legacy_recognize_feature]))


def _run(args) -> dict:
    from importlib.metadata import version

    from erp_v2.importer.identity_cli import _manifest, _safe_engine, _sha

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
    bindings = dict(
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        list_sha256=args.list_sha256,
        snapshot_sha256=snapshot_sha,
        candidate_revision=manifest.candidate_revision,
        tenant_id=str(tenant),
        approver_party_id=str(actor),
        source_owner="legacy-erp",
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
                    )
        finally:
            destination_engine.dispose()
    finally:
        source_engine.dispose()


def _dump(fd: int, payload: dict) -> None:
    with os.fdopen(os.dup(fd), "w") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _receipt(path: Path) -> int:
    _require(path.parent.resolve(strict=True) == Path(os.path.abspath(path.parent)), "receipt_symlink_forbidden")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.legacy_ap.recognize_cli")
    for name in ("manifest", "snapshot-file", "list", "receipt", "plan-file"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--list-sha256", required=True)
    parser.add_argument("--approver-party-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed-plan-sha256")
    args = parser.parse_args(argv)
    try:
        _require(_authorized(args), "explicit_apply_authorization_required")
        fd = _receipt(args.receipt)
        args.plan_fd = _receipt(args.plan_file)  # private per-invoice plan: 0600, O_EXCL, no symlinks
    except Exception:
        print("legacy recognition refused: authorization or private receipt path")
        return 2
    with os.fdopen(fd, "w") as stream, os.fdopen(args.plan_fd, "a"):
        try:
            payload = _run(args)
            code = 0 if payload["counts"]["refused"] == 0 else 3
        except RecognizeRefusal as exc:
            payload, code = dict(production_ready=False, refusal=exc.reason), 2
        except Exception:
            payload, code = dict(production_ready=False, refusal="recognize_command_refused"), 2
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    counts = payload.get("counts", {})
    print(
        "legacy recognition: "
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
