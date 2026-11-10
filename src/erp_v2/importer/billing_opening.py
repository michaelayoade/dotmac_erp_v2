"""Legacy AR -> billing opening-manifest translation (design + builder).

Billing's opening boundary is evidence, not row copies: each legacy
invoice becomes an OpeningReceivableItemV1 (original document
snapshot + exact historical discharges + residual), unallocated
customer payments become opening cash lots, and everything that
cannot be represented exactly becomes a named BLOCKER — never a
rounded, guessed or dropped row. One call translates one partition
(one billing account / currency); the caller groups by partition.

Honesty rules encoded here:
- Money converts to minor units exactly; a value needing rounding
  is a blocker (mirrors the module's own storage rule).
- Collection evidence is `unknown_unverified`: legacy ERP cannot
  prove verified service periods or due-date provenance, so
  automated collection stays disallowed until product evidence
  exists. We do not fabricate verification.
- Over-allocated invoices and payments allocated to invoices
  outside the partition are blockers/cash-review items, never
  silently netted.
- ERP v2 is never the receivables authority for a Sub-originated
  record (F1). Legacy ERP mirrors Sub invoices into its AR tagged
  `dotmac_sub_id`; Sub's own Billing owns those open items. Every
  invoice (credit notes included) whose `dotmac_sub_id` IS NOT NULL
  is excluded with its allocations and payments, and counted in
  `SubOriginExclusions` by family with source keys. Rows that do not
  carry the field are refused: missing is never treated as native.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from dotmac_billing import (
    ImportOpeningAccountV2,
    OpeningAccountImportVerifierV2,
    OpeningTargetV1,
    opening_manifest_fingerprint_v2,
    preview_opening_account_import_v2,
)
from dotmac_billing.contracts import (
    DueDateBasisV1,
    PartyDocumentSnapshotV1,
    PaymentInstructionsSnapshotV1,
    PresentationAssetReferenceV1,
    ServicePeriodEvidenceV1,
    ServicePeriodStatus,
)
from dotmac_billing.opening_contracts import (
    OpeningCaptureV1,
    OpeningCashLotV1,
    OpeningConsumptionV1,
    OpeningDispositionV1,
    OpeningLaneControlsV1,
    OpeningPartitionV1,
    OpeningPopulationCountsV1,
    OpeningSourceIdentityV1,
)
from dotmac_billing.opening_receivable_contracts import (
    OpeningCollectionEvidenceV1,
    OpeningHistoricalDischargeV1,
    OpeningManifestV2,
    OpeningOriginalDocumentV1,
    OpeningReceivableItemV1,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money

SOURCE_SYSTEM = "legacy-erp"
MAPPING_VERSION = "erp-billing-opening/1"
_VOID_STATUSES = frozenset({"voided", "void", "cancelled", "draft"})


class OpeningTranslationError(RuntimeError):
    """A structural refusal (bad partition inputs), not a per-row blocker."""


@dataclass(frozen=True)
class OpeningBlocker:
    source_key: str
    reason: str


@dataclass(frozen=True)
class OpeningTranslationContext:
    partition: OpeningPartitionV1
    seller: PartyDocumentSnapshotV1
    customer_snapshot: Callable[[str], PartyDocumentSnapshotV1 | None]
    payment_instructions: PaymentInstructionsSnapshotV1
    document_profile_code: str = "legacy-erp-invoice"
    document_profile_version: str = "1"
    locale: str = "en-NG"
    timezone: str = "Africa/Lagos"
    source_authority: str = "legacy-erp-readonly-export"


@dataclass(frozen=True)
class ExcludedPaymentSplit:
    """A mixed payment: ERP-native allocations kept, Sub-origin ones named.

    Money is never re-attributed: `excluded_amount_minor` stays with
    Sub's Billing and is removed from the ERP lot's gross.
    """

    payment_id: str
    excluded_allocations: tuple[str, ...]  # "<payment_id>:<invoice_id>"
    excluded_amount_minor: int
    kept_allocations: tuple[str, ...]


@dataclass(frozen=True)
class SubOriginExclusions:
    """Sub-origin rows kept OUT of the ERP v2 opening, by family.

    Every tuple is sorted, so the receipt is deterministic.
    """

    invoices: tuple[str, ...] = ()
    credit_notes: tuple[str, ...] = ()
    allocations: tuple[str, ...] = ()  # "<payment_id>:<invoice_id>"
    payments: tuple[str, ...] = ()
    mixed_payments: tuple[ExcludedPaymentSplit, ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "invoices": len(self.invoices),
            "credit_notes": len(self.credit_notes),
            "allocations": len(self.allocations),
            "payments": len(self.payments),
            "mixed_payments": len(self.mixed_payments),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "counts": self.counts(),
            "invoices": list(self.invoices),
            "credit_notes": list(self.credit_notes),
            "allocations": list(self.allocations),
            "payments": list(self.payments),
            "mixed_payments": [
                {
                    "payment_id": m.payment_id,
                    "excluded_allocations": list(m.excluded_allocations),
                    "excluded_amount_minor": m.excluded_amount_minor,
                    "kept_allocations": list(m.kept_allocations),
                }
                for m in self.mixed_payments
            ],
        }


SUB_ORIGIN_FIELD = "dotmac_sub_id"


def _is_sub_origin(row: Mapping[str, object], kind: str, key: str) -> bool:
    """Legacy semantics: Sub-origin is `dotmac_sub_id IS NOT NULL`.

    Only an explicit null (None) is ERP-native; an empty or blank
    string is NOT NULL in legacy and therefore Sub-origin. A missing
    field is refused, never defaulted.
    """

    if SUB_ORIGIN_FIELD not in row:
        raise OpeningTranslationError(
            f"{kind} {key}: row lacks {SUB_ORIGIN_FIELD}; refusing (missing is not ERP-native)"
        )
    return row[SUB_ORIGIN_FIELD] is not None


@dataclass
class OpeningTranslation:
    exclusions: SubOriginExclusions = field(default_factory=SubOriginExclusions)
    items: tuple[OpeningReceivableItemV1, ...] = ()
    cash_lots: tuple[OpeningCashLotV1, ...] = ()
    blockers: list[OpeningBlocker] = field(default_factory=list)
    dispositions: tuple[OpeningDispositionV1, ...] = ()

    @property
    def open_ar_minor(self) -> int:
        return sum(i.residual_minor for i in self.items if i.residual_minor > 0)


def _minor(amount: Decimal, precision: int) -> int | None:
    """Exact minor-unit conversion; None when rounding would be needed."""

    scaled = amount * (Decimal(10) ** precision)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def _digest(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _money(amount: Decimal, currency: str, precision: int) -> Money:
    return Money(amount, Currency(currency, precision))


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _exclude_sub_origin(
    invoices: Sequence[Mapping[str, object]],
    allocations: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
    precision: int,
    blockers: list[OpeningBlocker],
) -> tuple[
    list[Mapping[str, object]],
    list[Mapping[str, object]],
    list[Mapping[str, object]],
    SubOriginExclusions,
]:
    """Split Sub-origin rows out of the legacy AR rows (F1)."""

    sub_invoice_ids: set[str] = set()
    sub_invoices: list[str] = []
    sub_credit_notes: list[str] = []
    native_invoices: list[Mapping[str, object]] = []
    for invoice in invoices:
        key = str(invoice["invoice_id"])
        if _is_sub_origin(invoice, "invoice", key):
            sub_invoice_ids.add(key)
            if str(invoice.get("invoice_type", "")).upper() == "CREDIT_NOTE":
                sub_credit_notes.append(key)
            else:
                sub_invoices.append(key)
        else:
            native_invoices.append(invoice)

    sub_payment_flag: dict[str, bool] = {}
    for payment in payments:
        pid = str(payment["payment_id"])
        sub_payment_flag[pid] = _is_sub_origin(payment, "payment", pid)

    kept_allocs: list[Mapping[str, object]] = []
    sub_allocs_by_payment: dict[str, list[Mapping[str, object]]] = {}
    kept_by_payment: dict[str, list[str]] = {}
    for alloc in allocations:
        pid = str(alloc["payment_id"])
        if str(alloc["invoice_id"]) in sub_invoice_ids:
            sub_allocs_by_payment.setdefault(pid, []).append(alloc)
        else:
            kept_allocs.append(alloc)
            kept_by_payment.setdefault(pid, []).append(f"{pid}:{alloc['invoice_id']}")

    excluded_payments: list[str] = []
    mixed: list[ExcludedPaymentSplit] = []
    native_payments: list[Mapping[str, object]] = []
    for payment in payments:
        pid = str(payment["payment_id"])
        sub_allocs = sub_allocs_by_payment.get(pid, [])
        kept = kept_by_payment.get(pid, [])
        if sub_allocs and not kept:
            excluded_payments.append(pid)  # every allocation targets Sub-origin invoices
            continue
        if not sub_allocs and not kept and sub_payment_flag[pid]:
            excluded_payments.append(pid)  # unallocated Sub-origin cash
            continue
        if sub_payment_flag[pid]:
            # Sub-tagged payment that also touches ERP-native invoices: the
            # origin of the cash is contradictory. Do not guess: block it.
            blockers.append(OpeningBlocker(pid, "Sub-origin payment allocated to ERP-native invoices"))
            continue
        if sub_allocs:
            amount_minor = 0
            for alloc in sub_allocs:
                minor = _minor(Decimal(str(alloc["amount_allocated"])), precision)
                if minor is None:
                    blockers.append(OpeningBlocker(pid, "excluded Sub-origin allocation is inexact"))
                else:
                    amount_minor += minor
            mixed.append(
                ExcludedPaymentSplit(
                    payment_id=pid,
                    excluded_allocations=tuple(sorted(f"{pid}:{a['invoice_id']}" for a in sub_allocs)),
                    excluded_amount_minor=amount_minor,
                    kept_allocations=tuple(sorted(kept)),
                )
            )
        native_payments.append(payment)

    excluded_alloc_keys = sorted(f"{pid}:{a['invoice_id']}" for pid, al in sub_allocs_by_payment.items() for a in al)
    exclusions = SubOriginExclusions(
        invoices=tuple(sorted(sub_invoices)),
        credit_notes=tuple(sorted(sub_credit_notes)),
        allocations=tuple(excluded_alloc_keys),
        payments=tuple(sorted(excluded_payments)),
        mixed_payments=tuple(sorted(mixed, key=lambda m: m.payment_id)),
    )
    return native_invoices, kept_allocs, native_payments, exclusions


def translate_partition(
    *,
    context: OpeningTranslationContext,
    invoices: Sequence[Mapping[str, object]],
    allocations: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
) -> OpeningTranslation:
    """Translate one partition's legacy AR rows into opening evidence.

    Invoice and payment rows MUST carry `dotmac_sub_id` (None =
    ERP-native; anything else, blank included, = Sub-origin and
    excluded); a row without the field is refused. Optional
    `invoice_type` identifies credit notes for the exclusion counts.
    Invoice rows: invoice_id, invoice_number, customer_key,
    invoice_date (datetime), due_date (date|None), currency_code,
    subtotal/tax_amount/total_amount (Decimal), status.
    Allocation rows: invoice_id, payment_id, amount_allocated,
    allocated_at (datetime). Payment rows: payment_id, amount,
    payment_date (datetime), reference, status.
    """

    partition = context.partition
    precision = partition.precision
    result = OpeningTranslation()
    invoices, allocations, payments, exclusions = _exclude_sub_origin(
        invoices, allocations, payments, precision, result.blockers
    )
    result.exclusions = exclusions
    dispositions: list[OpeningDispositionV1] = []
    items: list[OpeningReceivableItemV1] = []

    payments_by_id = {str(p["payment_id"]): p for p in payments}
    allocations_by_invoice: dict[str, list[Mapping[str, object]]] = {}
    allocated_by_payment: dict[str, Decimal] = {}
    for alloc in allocations:
        allocations_by_invoice.setdefault(str(alloc["invoice_id"]), []).append(alloc)
        pid = str(alloc["payment_id"])
        allocated_by_payment[pid] = allocated_by_payment.get(pid, Decimal(0)) + Decimal(str(alloc["amount_allocated"]))

    included_invoice_ids: set[str] = set()
    for invoice in invoices:
        key = str(invoice["invoice_id"])
        identity = OpeningSourceIdentityV1(kind="invoice", key=key)
        status = str(invoice.get("status", "")).lower()
        if str(invoice.get("currency_code")) != partition.currency:
            result.blockers.append(OpeningBlocker(key, "invoice currency does not match partition currency"))
            dispositions.append(OpeningDispositionV1(identity, partition, "blocked", "currency mismatch", 0))
            continue
        grand = Decimal(str(invoice["total_amount"]))
        grand_minor = _minor(grand, precision)
        if grand_minor is None:
            result.blockers.append(OpeningBlocker(key, "total_amount is not exact at currency precision"))
            dispositions.append(OpeningDispositionV1(identity, partition, "blocked", "inexact money", 0))
            continue
        if status in _VOID_STATUSES:
            dispositions.append(OpeningDispositionV1(identity, partition, "excluded", f"legacy status {status}", 0))
            continue
        included_invoice_ids.add(key)

        discharges: list[OpeningHistoricalDischargeV1] = []
        discharged_minor = 0
        invoice_blocked = False
        for alloc in allocations_by_invoice.get(key, []):
            amount = Decimal(str(alloc["amount_allocated"]))
            amount_minor = _minor(amount, precision)
            payment = payments_by_id.get(str(alloc["payment_id"]))
            if amount_minor is None or payment is None:
                result.blockers.append(OpeningBlocker(key, "allocation is inexact or its payment is missing"))
                invoice_blocked = True
                break
            economic_at = _as_utc(alloc["allocated_at"])  # type: ignore[arg-type]
            discharges.append(
                OpeningHistoricalDischargeV1(
                    reference=f"legacy-alloc:{alloc['payment_id']}:{key}",
                    kind="payment",
                    amount_minor=amount_minor,
                    economic_at=economic_at,
                    recorded_at=economic_at,
                    source_system=SOURCE_SYSTEM,
                    source_key=str(alloc["payment_id"]),
                    live_operation="legacy.invoice_payment_allocation",
                    live_identity=f"{alloc['payment_id']}:{key}",
                    mapping_version=MAPPING_VERSION,
                    ledger_ref=str(payment.get("reference") or alloc["payment_id"]),
                    evidence_digest=_digest({"alloc": dict(alloc)}),
                    tax_evidence_ref=f"legacy:{key}",
                    tax_snapshots=(),
                    reverses_ref=None,
                )
            )
            discharged_minor += amount_minor
        if invoice_blocked:
            dispositions.append(OpeningDispositionV1(identity, partition, "blocked", "bad allocation", 0))
            continue
        residual_minor = grand_minor - discharged_minor
        if residual_minor < 0:
            result.blockers.append(OpeningBlocker(key, "allocations exceed the invoice total (overpaid)"))
            dispositions.append(OpeningDispositionV1(identity, partition, "blocked", "overpaid", residual_minor))
            continue

        customer_key = str(invoice["customer_key"])
        customer = context.customer_snapshot(customer_key)
        original: OpeningOriginalDocumentV1 | None = None
        if customer is None:
            result.blockers.append(OpeningBlocker(key, "no customer document snapshot available"))
        else:
            subtotal = Decimal(str(invoice.get("subtotal", grand)))
            tax_total = Decimal(str(invoice.get("tax_amount", 0)))
            original = OpeningOriginalDocumentV1(
                document_number=str(invoice["invoice_number"]),
                issued_at=_as_utc(invoice["invoice_date"]),  # type: ignore[arg-type]
                documentary_reference=f"legacy:invoice:{key}",
                documentary_digest=_digest({"invoice": dict(invoice)}),
                subtotal=_money(subtotal, partition.currency, precision),
                tax_total=_money(tax_total, partition.currency, precision),
                grand_total=_money(grand, partition.currency, precision),
                seller=context.seller,
                customer=customer,
                party_tax_identities=(),
                tax_snapshots=(),
                fx_snapshot=None,
                tax_evidence_ref=f"legacy:{key}",
                fx_evidence_ref=f"legacy:{key}",
                document_profile_code=context.document_profile_code,
                document_profile_version=context.document_profile_version,
                payment_instructions=context.payment_instructions,
                brand_asset=PresentationAssetReferenceV1(status="none"),
                locale=context.locale,
                timezone=context.timezone,
            )
        due_date = invoice.get("due_date")
        due_at = (
            datetime(due_date.year, due_date.month, due_date.day, tzinfo=timezone.utc)  # type: ignore[attr-defined]
            if due_date is not None
            else None
        )
        item = OpeningReceivableItemV1(
            source_identity=identity,
            partition=partition,
            source_invoice_key=key,
            original=original,
            historical_discharges=tuple(discharges),
            residual_minor=residual_minor,
            collection=OpeningCollectionEvidenceV1(
                status="unknown_unverified",
                subject_ref=None,
                service_ref=None,
                collection_timing=None,
                service_period=ServicePeriodEvidenceV1(status=ServicePeriodStatus.UNKNOWN_UNVERIFIED),
                due_at=due_at,
                due_date_basis=DueDateBasisV1.unknown_unverified(
                    source_authority=context.source_authority,
                    evidence_ref=f"legacy:invoice:{key}",
                ),
                source_authority=context.source_authority,
                evidence_ref=f"legacy:invoice:{key}",
                mapping_version=MAPPING_VERSION,
            ),
            receivable_mapping_version=MAPPING_VERSION,
            tax_mapping_version=MAPPING_VERSION,
            source_resolver_version=MAPPING_VERSION,
            source_evidence_digest=_digest({"invoice": dict(invoice)}),
        )
        items.append(item)
        dispositions.append(
            OpeningDispositionV1(
                identity,
                partition,
                "receivable_open" if residual_minor > 0 else "receivable_discharged",
                "translated",
                residual_minor,
            )
        )

    split_excluded_minor = {m.payment_id: m.excluded_amount_minor for m in exclusions.mixed_payments}
    cash_lots: list[OpeningCashLotV1] = []
    for payment in payments:
        pid = str(payment["payment_id"])
        gross = Decimal(str(payment["amount"]))
        gross_minor = _minor(gross, precision)
        if gross_minor is None:
            result.blockers.append(OpeningBlocker(pid, "payment amount is not exact at currency precision"))
            continue
        allocated = allocated_by_payment.get(pid, Decimal(0))
        allocated_minor = _minor(allocated, precision) or 0
        gross_minor -= split_excluded_minor.get(pid, 0)  # Sub-origin part stays with Sub's Billing
        residual = gross_minor - allocated_minor
        if residual <= 0:
            continue  # fully allocated cash is discharge evidence, not a lot
        paid_at = _as_utc(payment["payment_date"])  # type: ignore[arg-type]
        cash_lots.append(
            OpeningCashLotV1(
                lot_id=f"legacy-payment:{pid}",
                source_identity=OpeningSourceIdentityV1(kind="payment", key=pid),
                partition=partition,
                source_system=SOURCE_SYSTEM,
                source_settlement_key=str(payment.get("reference") or pid),
                confirmation=None,
                gross_minor=gross_minor,
                residual_minor=residual,
                consumptions=tuple(
                    OpeningConsumptionV1(
                        reference=f"legacy-alloc:{pid}:{alloc['invoice_id']}",
                        kind="allocation",
                        amount_minor=_minor(Decimal(str(alloc["amount_allocated"])), precision) or 0,
                        economic_at=_as_utc(alloc["allocated_at"]),  # type: ignore[arg-type]
                        recorded_at=_as_utc(alloc["allocated_at"]),  # type: ignore[arg-type]
                        target_ref=str(alloc["invoice_id"]),
                        ledger_ref=None,
                        reverses_ref=None,
                    )
                    for alloc in allocations
                    if str(alloc["payment_id"]) == pid and str(alloc["invoice_id"]) in included_invoice_ids
                ),
            )
        )
        _ = paid_at

    result.items = tuple(items)
    result.cash_lots = tuple(cash_lots)
    result.dispositions = tuple(dispositions)
    return result


def build_manifest(
    *,
    tenant_id: object,
    capture: OpeningCaptureV1,
    translation: OpeningTranslation,
) -> OpeningManifestV2:
    """Assemble the V2 manifest for one translated partition."""

    partition = translation.items[0].partition if translation.items else None
    if partition is None:
        raise OpeningTranslationError("cannot build a manifest with no translated items")
    population = tuple(d.identity for d in translation.dispositions)
    counts = OpeningPopulationCountsV1(
        population=len(population),
        eligible=len(translation.items),
        excluded=sum(1 for d in translation.dispositions if d.disposition == "excluded"),
        blocking=len(translation.blockers),
    )
    controls = OpeningLaneControlsV1(
        partition=partition,
        collectible_receivable_minor=0,  # unknown_unverified evidence: never auto-collectible
        available_cash_credit_minor=sum(lot.residual_minor for lot in translation.cash_lots),
        available_non_cash_credit_minor=0,
        unapplied_customer_credit_minor=sum(lot.residual_minor for lot in translation.cash_lots),
        prepaid_funding_reserved_minor=0,
        prepaid_funding_consumed_minor=0,
        written_off_total_minor=0,
        refunded_total_minor=0,
        adjustment_total_minor=0,
        open_ar_minor=translation.open_ar_minor,
    )
    return OpeningManifestV2(
        scope=TenantScope(tenant_id),  # type: ignore[arg-type]
        capture=capture,
        population=population,
        dispositions=translation.dispositions,
        counts=counts,
        controls=(controls,),
        cash_lots=translation.cash_lots,
        receivables=translation.items,
    )


@dataclass(frozen=True)
class OpeningPreflight:
    """Verifier round-trip evidence for ONE partition. Never executes."""

    manifest_fingerprint: str
    idempotency_key: str
    admissible: bool  # the module preview's own verdict
    importable: bool  # admissible AND zero local translation blockers
    cash_residual_minor: int
    receivable_residual_minor: int
    preview_blocker_codes: tuple[str, ...]
    translation_blockers: tuple[OpeningBlocker, ...]


def build_import_command(
    *,
    manifest: OpeningManifestV2,
    target: OpeningTargetV1,
    migration_authority_ref: str,
    occurred_at: datetime,
    idempotency_key: str | None = None,
) -> ImportOpeningAccountV2:
    """Assemble the V2 command after local target/partition agreement.

    `target` is read from the destination at rehearsal (the module's
    `get_opening_cash_target`); `migration_authority_ref` is the
    named approval reference. Neither is derivable here, and neither
    is ever defaulted: a mismatch between target, partition and
    manifest scope is a structural refusal, not a per-row blocker.
    """

    if not manifest.receivables:
        raise OpeningTranslationError("cannot build an import command with no receivables")
    partition = manifest.receivables[0].partition
    mismatches = []
    if target.account_id != partition.account_id:
        mismatches.append("account_id")
    if target.currency != partition.currency:
        mismatches.append("currency")
    if target.precision != partition.precision:
        mismatches.append("precision")
    if target.scope != manifest.scope:
        mismatches.append("scope")
    if mismatches:
        raise OpeningTranslationError(f"opening target disagrees with manifest partition/scope: {mismatches}")
    if not migration_authority_ref:
        raise OpeningTranslationError("migration_authority_ref is required (named approval, never defaulted)")
    fingerprint = opening_manifest_fingerprint_v2(manifest)
    return ImportOpeningAccountV2(
        scope=manifest.scope,
        manifest=manifest,
        partition=partition,
        target=target,
        migration_authority_ref=migration_authority_ref,
        idempotency_key=idempotency_key or f"erp-opening:{partition.source_account_key}:{fingerprint[:16]}",
        occurred_at=_as_utc(occurred_at),
    )


def preflight_opening_import(
    *,
    translation: OpeningTranslation,
    manifest: OpeningManifestV2,
    target: OpeningTargetV1,
    migration_authority_ref: str,
    occurred_at: datetime,
    verifier: OpeningAccountImportVerifierV2 | None = None,
) -> OpeningPreflight:
    """Run the module's own preview + verifier round-trip. Pure.

    With no verifier configured the module preview itself reports
    `verifier_unavailable` - that is the honest rehearsal state
    until the signed-grant verifier (Starter billing M1, Sub-first)
    is injected. This function never executes the import.
    """

    fingerprint = opening_manifest_fingerprint_v2(manifest)
    command = build_import_command(
        manifest=manifest,
        target=target,
        migration_authority_ref=migration_authority_ref,
        occurred_at=occurred_at,
    )
    preview = preview_opening_account_import_v2(command, verifier=verifier)
    if preview.manifest_fingerprint != fingerprint:
        raise OpeningTranslationError("module preview fingerprint disagrees with local fingerprint")
    return OpeningPreflight(
        manifest_fingerprint=fingerprint,
        idempotency_key=command.idempotency_key,
        admissible=preview.admissible,
        importable=preview.admissible and not translation.blockers,
        cash_residual_minor=preview.cash_residual_minor,
        receivable_residual_minor=preview.receivable_residual_minor,
        preview_blocker_codes=tuple(b.code for b in preview.blockers),
        translation_blockers=tuple(translation.blockers),
    )


__all__ = [
    "MAPPING_VERSION",
    "SOURCE_SYSTEM",
    "SUB_ORIGIN_FIELD",
    "ExcludedPaymentSplit",
    "OpeningBlocker",
    "OpeningPreflight",
    "OpeningTranslation",
    "OpeningTranslationContext",
    "OpeningTranslationError",
    "SubOriginExclusions",
    "build_import_command",
    "build_manifest",
    "preflight_opening_import",
    "translate_partition",
]
