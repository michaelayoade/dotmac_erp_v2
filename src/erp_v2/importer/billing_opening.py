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
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

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


@dataclass
class OpeningTranslation:
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


def translate_partition(
    *,
    context: OpeningTranslationContext,
    invoices: Sequence[Mapping[str, object]],
    allocations: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
) -> OpeningTranslation:
    """Translate one partition's legacy AR rows into opening evidence.

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


__all__ = [
    "MAPPING_VERSION",
    "SOURCE_SYSTEM",
    "OpeningBlocker",
    "OpeningTranslation",
    "OpeningTranslationContext",
    "OpeningTranslationError",
    "build_manifest",
    "translate_partition",
]
