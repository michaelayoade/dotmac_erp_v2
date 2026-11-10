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


# Splynx-era rows (`splynx_id` set, `dotmac_sub_id` NULL) are BLOCKED, not
# imported, until Michael rules (M13). One named policy so it can be lifted.
SPLYNX_ERA_POLICY = "block"  # "block" | "native" (lift only after the M13 ruling)
SPLYNX_ERA_REASON = "splynx_era_pending_ruling"
SPLYNX_FIELD = "splynx_id"
SUB_ORIGIN_FIELD = "dotmac_sub_id"
SUB_ORIGIN_REASON = "sub_origin"
UNDER_REVIEW_REASON = "discharged by payment under review"


@dataclass(frozen=True)
class ExcludedPaymentSplit:
    """Evidence for an ERP-recorded payment applied to Sub-origin invoices.

    The payment is a blocker for the exceptions review; nothing is split
    or re-attributed. All amounts are minor units.
    """

    payment_id: str
    gross_minor: int
    sub_share_minor: int
    native_share_minor: int
    remainder_minor: int
    sub_allocations: tuple[str, ...]  # "<payment_id>:<invoice_id>"
    native_allocations: tuple[str, ...]


@dataclass(frozen=True)
class SubOriginExclusions:
    """Sub-origin rows kept OUT of the ERP v2 opening, by family.

    Every tuple is sorted, so the receipt is deterministic.
    """

    invoices: tuple[str, ...] = ()
    credit_notes: tuple[str, ...] = ()
    allocations: tuple[str, ...] = ()  # "<payment_id>:<invoice_id>"
    payments: tuple[str, ...] = ()  # Sub-tagged payments excluded whole
    mixed_payments: tuple[ExcludedPaymentSplit, ...] = ()  # ERP cash on Sub invoices (blocked)
    totals_minor: Mapping[str, int] = field(default_factory=dict)

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
            "totals_minor": dict(sorted(self.totals_minor.items())),
            "invoices": list(self.invoices),
            "credit_notes": list(self.credit_notes),
            "allocations": list(self.allocations),
            "payments": list(self.payments),
            "mixed_payments": [
                {
                    "payment_id": m.payment_id,
                    "gross_minor": m.gross_minor,
                    "sub_share_minor": m.sub_share_minor,
                    "native_share_minor": m.native_share_minor,
                    "remainder_minor": m.remainder_minor,
                    "sub_allocations": list(m.sub_allocations),
                    "native_allocations": list(m.native_allocations),
                }
                for m in self.mixed_payments
            ],
        }


def _tag(row: Mapping[str, object], fld: str, kind: str, key: str) -> object:
    if fld not in row:
        raise OpeningTranslationError(f"{kind} {key}: row lacks {fld}; refusing (missing is not ERP-native)")
    return row[fld]


def _is_sub_origin(row: Mapping[str, object], kind: str, key: str) -> bool:
    """Legacy semantics: Sub-origin is `dotmac_sub_id IS NOT NULL`.

    Only an explicit null (None) is ERP-native; a blank string is NOT
    NULL in legacy and therefore Sub-origin. A missing field is refused.
    """

    return _tag(row, SUB_ORIGIN_FIELD, kind, key) is not None


def _is_splynx_era(row: Mapping[str, object], kind: str, key: str) -> bool:
    """`splynx_id` set on a row that is not Sub-origin (refuses a missing field)."""

    return _tag(row, SPLYNX_FIELD, kind, key) is not None


@dataclass
class OpeningTranslation:
    exclusions: SubOriginExclusions = field(default_factory=SubOriginExclusions)
    census: dict[str, object] = field(default_factory=dict)
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


@dataclass
class _Prep:
    invoices: list[Mapping[str, object]]  # candidates for translation (native, not pre-blocked)
    allocations: list[Mapping[str, object]]  # allocations to those candidates
    payments: list[Mapping[str, object]]  # ERP payments still in play (blocked ones included)
    blocked_payments: dict[str, str]  # payment_id -> reason (no lot is produced)
    excluded_payments: set[str]
    invoice_outcome: dict[str, str]  # pre-decided invoice outcomes
    dispositions: list[OpeningDispositionV1]
    exclusions: SubOriginExclusions
    splynx_keys: dict[str, list[str]]
    sub_customers_on_native: list[str]


def _unusable_invoice(invoice: Mapping[str, object], currency: str, precision: int) -> bool:
    """Void, wrong currency or inexact: a payment allocated to it cannot be trusted."""

    if str(invoice.get("status", "")).lower() in _VOID_STATUSES:
        return True
    if str(invoice.get("currency_code")) != currency:
        return True
    return _minor(Decimal(str(invoice["total_amount"])), precision) is None


def _classify_sub_origin(
    invoices: Sequence[Mapping[str, object]],
    allocations: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
    partition: OpeningPartitionV1,
    blockers: list[OpeningBlocker],
) -> _Prep:
    """Split Sub-origin rows out of the legacy AR rows and fail closed (F1)."""

    precision = partition.precision
    for kind, rows, id_field in (("invoice", invoices, "invoice_id"), ("payment", payments, "payment_id")):
        seen: set[str] = set()
        for row in rows:
            row_key = str(row[id_field])
            if row_key in seen:
                raise OpeningTranslationError(f"duplicate {kind} id {row_key} in export")
            seen.add(row_key)

    dispositions: list[OpeningDispositionV1] = []
    invoice_outcome: dict[str, str] = {}
    sub_invoice_ids: set[str] = set()
    unusable_ids: set[str] = set()
    known_ids: set[str] = set()
    sub_invoices: list[str] = []
    sub_credit_notes: list[str] = []
    sub_invoice_minor = 0
    candidates: list[Mapping[str, object]] = []
    splynx_keys: dict[str, list[str]] = {"invoices": [], "payments": []}
    sub_customers: set[str] = set()
    for invoice in invoices:
        key = str(invoice["invoice_id"])
        known_ids.add(key)
        identity = OpeningSourceIdentityV1(kind="invoice", key=key)
        if _is_sub_origin(invoice, "invoice", key):
            sub_invoice_ids.add(key)
            invoice_outcome[key] = "excluded_sub_origin"
            minor = _minor(Decimal(str(invoice["total_amount"])), precision)
            sub_invoice_minor += minor or 0
            if str(invoice.get("invoice_type", "")).upper() == "CREDIT_NOTE":
                sub_credit_notes.append(key)
            else:
                sub_invoices.append(key)
            dispositions.append(OpeningDispositionV1(identity, partition, "excluded", SUB_ORIGIN_REASON, 0))
            continue
        if _is_splynx_era(invoice, "invoice", key):
            splynx_keys["invoices"].append(key)
            if SPLYNX_ERA_POLICY == "block":
                invoice_outcome[key] = "blocked"
                unusable_ids.add(key)
                blockers.append(OpeningBlocker(key, SPLYNX_ERA_REASON))
                dispositions.append(OpeningDispositionV1(identity, partition, "blocked", SPLYNX_ERA_REASON, 0))
                continue
        if invoice.get("customer_dotmac_sub_id") is not None:
            sub_customers.add(str(invoice["customer_key"]))
        if _unusable_invoice(invoice, partition.currency, precision):
            unusable_ids.add(key)
        candidates.append(invoice)

    sub_alloc_keys: list[str] = []
    alloc_by_payment: dict[str, list[Mapping[str, object]]] = {}
    for alloc in allocations:
        alloc_by_payment.setdefault(str(alloc["payment_id"]), []).append(alloc)

    blocked_payments: dict[str, str] = {}
    excluded_payments: list[str] = []
    excluded_payment_minor = 0
    splits: list[ExcludedPaymentSplit] = []
    erp_payments: list[Mapping[str, object]] = []
    for payment in payments:
        pid = str(payment["payment_id"])
        sub_tagged = _is_sub_origin(payment, "payment", pid)
        era = (not sub_tagged) and _is_splynx_era(payment, "payment", pid)
        gross_minor = _minor(Decimal(str(payment["amount"])), precision)
        allocs = alloc_by_payment.get(pid, [])
        to_sub = [a for a in allocs if str(a["invoice_id"]) in sub_invoice_ids]
        absent = [a for a in allocs if str(a["invoice_id"]) not in known_ids]
        unusable = [a for a in allocs if str(a["invoice_id"]) in unusable_ids]
        to_native = [a for a in allocs if a not in to_sub and a not in absent and a not in unusable]
        sub_alloc_keys += [f"{pid}:{a['invoice_id']}" for a in to_sub]

        if sub_tagged and not (absent or unusable or to_native):
            excluded_payments.append(pid)  # Sub-tagged and every allocation (if any) is Sub-origin
            excluded_payment_minor += gross_minor or 0
            dispositions.append(
                OpeningDispositionV1(
                    OpeningSourceIdentityV1(kind="payment", key=pid), partition, "excluded", SUB_ORIGIN_REASON, 0
                )
            )
            continue
        reason: str | None = None
        if sub_tagged:
            reason = "Sub-origin payment allocated to ERP-native or unverifiable invoices"
        elif era and SPLYNX_ERA_POLICY == "block":
            splynx_keys["payments"].append(pid)
            reason = SPLYNX_ERA_REASON
        elif absent:
            reason = "allocation targets invoice absent from export"
        elif unusable:
            reason = "allocation targets void or blocked invoice"
        elif to_sub:
            sub_minor = sum(_minor(Decimal(str(a["amount_allocated"])), precision) or 0 for a in to_sub)
            native_minor = sum(_minor(Decimal(str(a["amount_allocated"])), precision) or 0 for a in to_native)
            gross = gross_minor or 0
            splits.append(
                ExcludedPaymentSplit(
                    payment_id=pid,
                    gross_minor=gross,
                    sub_share_minor=sub_minor,
                    native_share_minor=native_minor,
                    remainder_minor=gross - sub_minor - native_minor,
                    sub_allocations=tuple(sorted(f"{pid}:{a['invoice_id']}" for a in to_sub)),
                    native_allocations=tuple(sorted(f"{pid}:{a['invoice_id']}" for a in to_native)),
                )
            )
            reason = (
                "ERP-recorded cash applied to Sub-origin invoice: exceptions review "
                f"(gross {gross}, sub {sub_minor}, native {native_minor}, "
                f"unapplied {gross - sub_minor - native_minor} minor)"
            )
        if reason is not None:
            blocked_payments[pid] = reason
            blockers.append(OpeningBlocker(pid, reason))
        erp_payments.append(payment)

    kept_allocs = [a for a in allocations if str(a["invoice_id"]) not in sub_invoice_ids]
    candidate_ids = {str(c["invoice_id"]) for c in candidates}
    kept_allocs = [a for a in kept_allocs if str(a["invoice_id"]) in candidate_ids]
    exclusions = SubOriginExclusions(
        invoices=tuple(sorted(sub_invoices)),
        credit_notes=tuple(sorted(sub_credit_notes)),
        allocations=tuple(sorted(sub_alloc_keys)),
        payments=tuple(sorted(excluded_payments)),
        mixed_payments=tuple(sorted(splits, key=lambda m: m.payment_id)),
        totals_minor={"invoices_and_credit_notes": sub_invoice_minor, "payments": excluded_payment_minor},
    )
    return _Prep(
        invoices=candidates,
        allocations=kept_allocs,
        payments=erp_payments,
        blocked_payments=blocked_payments,
        excluded_payments=set(excluded_payments),
        invoice_outcome=invoice_outcome,
        dispositions=dispositions,
        exclusions=exclusions,
        splynx_keys={k: sorted(v) for k, v in splynx_keys.items()},
        sub_customers_on_native=sorted(sub_customers),
    )


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
    source_invoice_count = len(invoices)
    source_payment_count = len(payments)
    prep = _classify_sub_origin(invoices, allocations, payments, partition, result.blockers)
    invoices, allocations, payments = prep.invoices, prep.allocations, prep.payments
    exclusions = prep.exclusions
    result.exclusions = exclusions
    blocked_payments = prep.blocked_payments
    invoice_outcome = prep.invoice_outcome
    dispositions: list[OpeningDispositionV1] = list(prep.dispositions)
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
            invoice_outcome[key] = "blocked"
            continue
        grand = Decimal(str(invoice["total_amount"]))
        grand_minor = _minor(grand, precision)
        if grand_minor is None:
            result.blockers.append(OpeningBlocker(key, "total_amount is not exact at currency precision"))
            dispositions.append(OpeningDispositionV1(identity, partition, "blocked", "inexact money", 0))
            invoice_outcome[key] = "blocked"
            continue
        if status in _VOID_STATUSES:
            dispositions.append(OpeningDispositionV1(identity, partition, "excluded", f"legacy status {status}", 0))
            invoice_outcome[key] = "excluded_void"
            continue
        included_invoice_ids.add(key)

        discharges: list[OpeningHistoricalDischargeV1] = []
        discharged_minor = 0
        invoice_blocked = False
        for alloc in allocations_by_invoice.get(key, []):
            amount = Decimal(str(alloc["amount_allocated"]))
            amount_minor = _minor(amount, precision)
            payment = payments_by_id.get(str(alloc["payment_id"]))
            if str(alloc["payment_id"]) in blocked_payments:
                result.blockers.append(OpeningBlocker(key, UNDER_REVIEW_REASON))
                invoice_blocked = True
                break
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
            invoice_outcome[key] = "blocked"
            continue
        residual_minor = grand_minor - discharged_minor
        if residual_minor < 0:
            result.blockers.append(OpeningBlocker(key, "allocations exceed the invoice total (overpaid)"))
            dispositions.append(OpeningDispositionV1(identity, partition, "blocked", "overpaid", residual_minor))
            invoice_outcome[key] = "blocked"
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
        invoice_outcome[key] = "translated"
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
    payment_outcome: dict[str, str] = {p: "blocked" for p in blocked_payments}
    for payment in payments:
        pid = str(payment["payment_id"])
        if pid in blocked_payments:
            continue  # already a named blocker (exceptions review); no lot
        gross = Decimal(str(payment["amount"]))
        gross_minor = _minor(gross, precision)
        if gross_minor is None:
            result.blockers.append(OpeningBlocker(pid, "payment amount is not exact at currency precision"))
            payment_outcome[pid] = "blocked"
            continue
        allocated = allocated_by_payment.get(pid, Decimal(0))
        allocated_minor = _minor(allocated, precision) or 0
        residual = gross_minor - allocated_minor
        if residual < 0:
            result.blockers.append(OpeningBlocker(pid, "payment is over-allocated (negative cash residual)"))
            payment_outcome[pid] = "blocked"
            continue
        if residual == 0:
            payment_outcome[pid] = "fully_allocated"
            continue  # fully allocated cash is discharge evidence, not a lot
        payment_outcome[pid] = "lot"
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

    for pid in prep.excluded_payments:
        payment_outcome[pid] = "excluded_sub_origin"
    result.census = _census(
        prep,
        source_invoice_count,
        source_payment_count,
        invoice_outcome,
        payment_outcome,
    )
    result.items = tuple(items)
    result.cash_lots = tuple(cash_lots)
    result.dispositions = tuple(dispositions)
    return result


def _census(
    prep: _Prep,
    source_invoices: int,
    source_payments: int,
    invoice_outcome: Mapping[str, str],
    payment_outcome: Mapping[str, str],
) -> dict[str, object]:
    """Per-family balance: legacy count = translated + excluded + blocked.

    Raises OpeningTranslationError when it does not balance.
    """

    def tally(outcomes: Mapping[str, str], keys: Sequence[str]) -> dict[str, int]:
        out = dict.fromkeys(keys, 0)
        for value in outcomes.values():
            out[value] = out.get(value, 0) + 1
        return out

    invoices = {
        "source": source_invoices,
        **tally(invoice_outcome, ("translated", "excluded_void", "excluded_sub_origin", "blocked")),
    }
    payments = {
        "source": source_payments,
        **tally(payment_outcome, ("lot", "fully_allocated", "excluded_sub_origin", "blocked")),
    }
    for family, tallies in (("invoices", invoices), ("payments", payments)):
        accounted = sum(v for k, v in tallies.items() if k != "source")
        if accounted != tallies["source"]:
            raise OpeningTranslationError(
                f"census does not balance for {family}: source {tallies['source']} != accounted {accounted}"
            )
    return {
        "balanced": True,
        "invoices": invoices,
        "payments": payments,
        "splynx_era": {
            "policy": SPLYNX_ERA_POLICY,
            "reason": SPLYNX_ERA_REASON,
            "invoices": prep.splynx_keys["invoices"],
            "payments": prep.splynx_keys["payments"],
        },
        "sub_tagged_customers_on_native_invoices": prep.sub_customers_on_native,
    }


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
    "SPLYNX_ERA_POLICY",
    "SPLYNX_ERA_REASON",
    "SUB_ORIGIN_FIELD",
    "UNDER_REVIEW_REASON",
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
