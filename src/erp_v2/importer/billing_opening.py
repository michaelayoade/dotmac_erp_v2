"""Legacy AR -> billing opening translation: ONE complete manifest (AR opening admission, rev 2).

Billing's opening boundary is evidence, not row copies. a12 binds ONE window to ONE complete
`OpeningManifestV2`; every import command carries the whole manifest and its `partition` (billing account x
currency) only selects. A partition is one legacy customer x currency:

- every open legacy invoice (residual = total - legacy allocations, > 0) is one `OpeningReceivableItemV1` with
  its legacy allocations as exact historical discharges;
- every payment that discharges an item, or carries unallocated cash, is one `OpeningCashLotV1` (gross,
  consumptions = its allocations, residual = unallocated cash). A payment-kind discharge without its lot is
  refused by billing (`historical_cash_source_unclaimed`);
- origin is provenance, not a rule (v1677): native, Sub-origin and Splynx rows are all translated, with the same
  identity. Origin, `dotmac_sub_id` and `splynx_id` live in the row (hence the evidence digest), never in the
  identity. This REVERSES F1 (efe14457): no Sub-origin exclusion, no Splynx-era block.

Honesty rules kept: exact minor units (rounding is a blocker); `unknown_unverified` collection evidence (B9-1c:
face value, no collection proof); required provenance columns (a missing column refuses the translation).

a12 refuses the whole manifest on any non-carryable row, and holds are operational only.

AS-IS rework (replan section 2.3, amended by the billing owner's a13 answers). Billing a13 answers P-1 = NO: a
customer x currency partition is the smallest holdable unit (`manifest_noncarryable_row`,
opening_variance_validation.py:127-132; no variance reason accepts source kind credit_note,
opening_variance_contracts.py:107-157; a `customer_credit` variance is refused for a customer with cash lots,
opening_variance_validation.py:149-157). So a row is NEVER credit_notes inside billing. Two levels:

- an UNAPPLIED CREDIT NOTE (credit note or negative total, nonzero residual, no allocation links, nothing depends
  on it) is simply NOT PART of the billing manifest. It never enters the population, so it cannot make the manifest
  non-carryable; its partition is carried as legacy recorded it. The note becomes a migration work-queue item
  (`ar_unapplied_credit_note`: party, document number, amount) that Finance applies or issues after the flip with
  normal billing commands, and part of the itemised per-party GL-minus-subledger difference on 1400 that the B-14
  holding reclass moves to the AR holding account. A credit note whose residual is 0 is just closed history;
- every other blocker stays WHOLE-PARTITION (E35 `partition:`, GL to holding): `payment_allocation_outside_population`,
  `payment_overallocated`, `invoice_residual_nonpositive`, `amount_not_exact`, currency, a missing customer
  snapshot, and a credit note that HAS allocation links (excluding only it would orphan the discharges and lots
  that depend on it).
`translate_partition` reports blockers and unapplied credit notes; `build_complete_manifest` only accepts carryable
translations;
the census (`ar_census`) routes unapplied credit notes and excluded partitions to the register.

Identity kinds are `invoice` and `payment`: a12's variance ledger only resolves a variance whose source kind is in
its closed itemised set and equals the population identity, so `ar_invoice` / `ar_customer_payment` (revision 2
section 3) cannot be used while stored-balance variances are carried.
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
    OpeningCashConfirmationV1,
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
from dotmac_billing.opening_variance_contracts import OpeningPartitionMeasuresV1, OpeningVarianceV1
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money

SOURCE_SYSTEM = "legacy-erp"
MAPPING_VERSION = "erp-billing-opening/2"
INVOICE_KIND = "invoice"
PAYMENT_KIND = "payment"
RULES_DIGEST = hashlib.sha256(MAPPING_VERSION.encode()).hexdigest()

SUB_ORIGIN_FIELD = "dotmac_sub_id"
SPLYNX_FIELD = "splynx_id"
INVOICE_REQUIRED_FIELDS = (SUB_ORIGIN_FIELD, SPLYNX_FIELD)
PAYMENT_REQUIRED_FIELDS = (SUB_ORIGIN_FIELD, SPLYNX_FIELD)

# Named E35 reasons (one partition-level exclusion each).
UNAPPLIED_CREDIT_REASON = "credit_transfer_unsupported"
OVERALLOCATED_REASON = "invoice_residual_nonpositive"
OUTSIDE_POPULATION_REASON = "payment_allocation_outside_population"
INEXACT_REASON = "amount_not_exact"
CURRENCY_REASON = "currency_mismatch"
CUSTOMER_SNAPSHOT_REASON = "customer_snapshot_missing"
PAYMENT_OVERALLOCATED_REASON = "payment_overallocated"
STORED_VARIANCE_REASON = "stored_balance_difference"

ORIGINS = ("native", "dotmac_sub", "splynx")


class OpeningTranslationError(RuntimeError):
    """A structural refusal (bad partition inputs), not a per-row blocker."""


@dataclass(frozen=True)
class OpeningBlocker:
    source_key: str
    reason: str


@dataclass(frozen=True)
class OpeningUnappliedCreditNote:
    kind: str  # invoice
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
    partition: OpeningPartitionV1 | None = None
    census: dict[str, object] = field(default_factory=dict)
    items: tuple[OpeningReceivableItemV1, ...] = ()
    cash_lots: tuple[OpeningCashLotV1, ...] = ()
    blockers: list[OpeningBlocker] = field(default_factory=list)
    # Unapplied credit notes kept out of the manifest (P-1 = NO: billing holds no row); the partition is carried.
    unapplied_credit_notes: list[OpeningUnappliedCreditNote] = field(default_factory=list)
    dispositions: tuple[OpeningDispositionV1, ...] = ()
    variances: tuple[OpeningVarianceV1, ...] = ()
    stored_balance_minor: int = 0
    rebuild_minor: int = 0
    origin_residual_minor: dict[str, int] = field(default_factory=dict)
    closed_invoices: tuple[str, ...] = ()
    ignored_payments: tuple[str, ...] = ()
    # Classification evidence kept even when the partition is excluded (the census needs it for E35).
    item_keys: tuple[str, ...] = ()
    lot_keys: tuple[str, ...] = ()
    residual_minor_by_invoice: dict[str, int] = field(default_factory=dict)
    gross_minor_by_key: dict[str, int] = field(default_factory=dict)
    payment_residual_minor: dict[str, int] = field(default_factory=dict)

    @property
    def open_ar_minor(self) -> int:
        return sum(i.residual_minor for i in self.items)

    @property
    def carryable(self) -> bool:
        return not self.blockers


def origin_of(row: Mapping[str, object]) -> str:
    """Provenance only: Sub-tagged, else Splynx-tagged, else native. Never a rule."""

    if row.get(SUB_ORIGIN_FIELD) is not None:
        return "dotmac_sub"
    if row.get(SPLYNX_FIELD) is not None:
        return "splynx"
    return "native"


def _require_fields(kind: str, rows: Sequence[Mapping[str, object]], id_field: str, required: Sequence[str]) -> None:
    seen: set[str] = set()
    for row in rows:
        key = str(row[id_field])
        for name in required:
            if name not in row:
                raise OpeningTranslationError(f"{kind} {key}: row lacks {name}; refusing (missing is not native)")
        if key in seen:
            raise OpeningTranslationError(f"duplicate {kind} id {key} in export")
        seen.add(key)


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
    """Translate ONE partition (customer x currency) of legacy AR rows into opening evidence.

    `invoices` are all the partition's receivable-posted invoices (open and closed; closed ones let an
    allocation to them be recognised as history, not as "outside the population"); `allocations` and
    `payments` are the partition's, already cut off at the capture date by the census. A payment
    allocated to an invoice that is NOT in `invoices` (absent, draft, void, other currency) is a blocker.

    Required export fields (a row missing one is refused): invoices and payments carry `dotmac_sub_id` and
    `splynx_id`. Invoice rows: invoice_id, invoice_number, customer_key, invoice_date (datetime), due_date,
    currency_code, subtotal/tax_amount/total_amount (Decimal); optional `balance_due` (the stored legacy
    figure, default = derived residual) and `invoice_type`. Allocation rows: invoice_id, payment_id,
    amount_allocated, allocated_at. Payment rows: payment_id, amount, payment_date, reference.
    """

    partition = context.partition
    precision = partition.precision
    _require_fields("invoice", invoices, "invoice_id", INVOICE_REQUIRED_FIELDS)
    _require_fields("payment", payments, "payment_id", PAYMENT_REQUIRED_FIELDS)
    result = OpeningTranslation(partition=partition)
    blockers = result.blockers
    credit_notes = result.unapplied_credit_notes

    invoice_by_id = {str(i["invoice_id"]): i for i in invoices}
    payment_by_id = {str(p["payment_id"]): p for p in payments}
    allocs_by_invoice: dict[str, list[Mapping[str, object]]] = {}
    allocs_by_payment: dict[str, list[Mapping[str, object]]] = {}
    for alloc in allocations:
        allocs_by_invoice.setdefault(str(alloc["invoice_id"]), []).append(alloc)
        allocs_by_payment.setdefault(str(alloc["payment_id"]), []).append(alloc)

    # --- invoices: residual = total - allocations; > 0 item, < 0 unapplied credit, 0 closed -------------
    item_ids: list[str] = []
    residuals: dict[str, int] = {}
    totals: dict[str, int] = {}
    closed: list[str] = []
    for key in sorted(invoice_by_id):
        invoice = invoice_by_id[key]
        if str(invoice.get("currency_code")) != partition.currency:
            blockers.append(OpeningBlocker(key, CURRENCY_REASON))
            continue
        total_minor = _minor(Decimal(str(invoice["total_amount"])), precision)
        alloc_minors = [_minor(Decimal(str(a["amount_allocated"])), precision) for a in allocs_by_invoice.get(key, [])]
        linked = bool(allocs_by_invoice.get(key))
        if total_minor is None or any(m is None for m in alloc_minors):
            blockers.append(OpeningBlocker(key, INEXACT_REASON))
            continue
        residual = total_minor - sum(m for m in alloc_minors if m is not None)
        totals[key] = total_minor
        residuals[key] = residual
        if residual != 0 and str(invoice.get("invoice_type", "")).upper() == "CREDIT_NOTE" and not linked:
            credit_notes.append(
                OpeningUnappliedCreditNote(INVOICE_KIND, key, UNAPPLIED_CREDIT_REASON)
            )  # unapplied credit note: out of the manifest
        elif residual > 0 and str(invoice.get("invoice_type", "")).upper() == "CREDIT_NOTE":
            blockers.append(OpeningBlocker(key, UNAPPLIED_CREDIT_REASON))
        elif residual > 0:
            item_ids.append(key)
        elif residual < 0:
            is_credit = total_minor < 0 or str(invoice.get("invoice_type", "")).upper() == "CREDIT_NOTE"
            if is_credit and not linked:
                credit_notes.append(OpeningUnappliedCreditNote(INVOICE_KIND, key, UNAPPLIED_CREDIT_REASON))
            else:
                blockers.append(OpeningBlocker(key, UNAPPLIED_CREDIT_REASON if is_credit else OVERALLOCATED_REASON))
        else:
            closed.append(key)
    result.closed_invoices = tuple(closed)
    item_set = set(item_ids)

    # --- payments: a lot for every payment that discharges an item or holds unallocated cash ----------
    lot_pids: list[str] = []
    ignored: list[str] = []
    payment_gross: dict[str, int] = {}
    payment_residual: dict[str, int] = {}
    for pid in sorted(payment_by_id):
        payment = payment_by_id[pid]
        gross = _minor(Decimal(str(payment["amount"])), precision)
        allocs = allocs_by_payment.get(pid, [])
        alloc_minors = [_minor(Decimal(str(a["amount_allocated"])), precision) for a in allocs]
        if gross is None or any(m is None for m in alloc_minors):
            blockers.append(OpeningBlocker(pid, INEXACT_REASON))
            continue
        if any(str(a["invoice_id"]) not in invoice_by_id for a in allocs):
            blockers.append(OpeningBlocker(pid, OUTSIDE_POPULATION_REASON))
            continue
        remainder = gross - sum(m for m in alloc_minors if m is not None)
        if remainder < 0:
            blockers.append(OpeningBlocker(pid, PAYMENT_OVERALLOCATED_REASON))
            continue
        touches_item = any(str(a["invoice_id"]) in item_set for a in allocs)
        if touches_item or remainder > 0:
            lot_pids.append(pid)
            payment_gross[pid] = gross
            payment_residual[pid] = remainder
        else:
            ignored.append(pid)  # fully allocated to history: not in the opening population
    result.ignored_payments = tuple(ignored)

    result.item_keys = tuple(item_ids)
    result.lot_keys = tuple(lot_pids)
    result.residual_minor_by_invoice = dict(residuals)
    result.gross_minor_by_key = {**totals, **payment_gross}
    result.payment_residual_minor = dict(payment_residual)

    # A discharge of an item by a payment that is blocked / has no lot is refused whole.
    lot_set = set(lot_pids)
    for key in item_ids:
        for alloc in allocs_by_invoice.get(key, []):
            if str(alloc["payment_id"]) not in lot_set:
                blockers.append(OpeningBlocker(key, OUTSIDE_POPULATION_REASON))
                break

    customer_snapshot_cache: dict[str, PartyDocumentSnapshotV1 | None] = {}
    for key in item_ids:
        customer_key = str(invoice_by_id[key]["customer_key"])
        if customer_key not in customer_snapshot_cache:
            customer_snapshot_cache[customer_key] = context.customer_snapshot(customer_key)
        if customer_snapshot_cache[customer_key] is None:
            blockers.append(OpeningBlocker(key, CUSTOMER_SNAPSHOT_REASON))

    if blockers:
        # Whole-partition exclusion: no items, lots, dispositions or variances are produced.
        result.unapplied_credit_notes = []
        result.census = {"invoices": len(invoices), "payments": len(payments), "carryable": False}
        return result
    result.unapplied_credit_notes = sorted(credit_notes, key=lambda h: (h.kind, h.source_key))

    items: list[OpeningReceivableItemV1] = []
    dispositions: list[OpeningDispositionV1] = []
    variances: list[OpeningVarianceV1] = []
    origin_residual = dict.fromkeys(ORIGINS, 0)
    stored_total = 0
    for key in item_ids:
        invoice = invoice_by_id[key]
        identity = OpeningSourceIdentityV1(kind=INVOICE_KIND, key=key)
        grand = Decimal(str(invoice["total_amount"]))
        residual = residuals[key]
        discharges = []
        for alloc in sorted(allocs_by_invoice.get(key, []), key=lambda a: str(a["payment_id"])):
            pid = str(alloc["payment_id"])
            amount_minor = _minor(Decimal(str(alloc["amount_allocated"])), precision) or 0
            economic_at = _as_utc(alloc["allocated_at"])  # type: ignore[arg-type]
            discharges.append(
                OpeningHistoricalDischargeV1(
                    reference=f"legacy-alloc:{pid}:{key}",
                    kind="payment",
                    amount_minor=amount_minor,
                    economic_at=economic_at,
                    recorded_at=economic_at,
                    source_system=SOURCE_SYSTEM,
                    source_key=pid,
                    live_operation="legacy.invoice_payment_allocation",
                    live_identity=f"{pid}:{key}",
                    mapping_version=MAPPING_VERSION,
                    ledger_ref=str(payment_by_id[pid].get("reference") or pid),
                    evidence_digest=_digest({"alloc": dict(alloc)}),
                    tax_evidence_ref=f"legacy:{key}",
                    tax_snapshots=(),
                    reverses_ref=None,
                )
            )
        customer = customer_snapshot_cache[str(invoice["customer_key"])]
        if customer is None:  # cannot happen: a missing snapshot blocked the partition above
            raise OpeningTranslationError(f"invoice {key}: customer snapshot vanished")
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
        items.append(
            OpeningReceivableItemV1(
                source_identity=identity,
                partition=partition,
                source_invoice_key=key,
                original=original,
                historical_discharges=tuple(discharges),
                residual_minor=residual,
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
        )
        dispositions.append(OpeningDispositionV1(identity, partition, "eligible", "translated", residual))
        origin_residual[origin_of(invoice)] += residual
        stored_minor = residual
        if invoice.get("balance_due") is not None:
            stored = _minor(Decimal(str(invoice["balance_due"])), precision)
            if stored is None:
                blockers.append(OpeningBlocker(key, INEXACT_REASON))
                continue
            stored_minor = stored
        stored_total += stored_minor
        if stored_minor != residual:
            variances.append(
                OpeningVarianceV1(
                    level="partition",
                    partition=partition,
                    currency=partition.currency,
                    precision=precision,
                    lane="receivable",
                    basis="stored_balance",
                    reason_code=STORED_VARIANCE_REASON,
                    source=identity,
                    amount_minor=stored_minor - residual,
                    evidence_digest=_digest({"invoice": dict(invoice), "derived_residual_minor": residual}),
                    rules_digest=RULES_DIGEST,
                )
            )
    if blockers:
        result.unapplied_credit_notes = []  # the whole partition is excluded; its rows are E35 partition members
        result.census = {"invoices": len(invoices), "payments": len(payments), "carryable": False}
        return result

    cash_lots: list[OpeningCashLotV1] = []
    for pid in lot_pids:
        payment = payment_by_id[pid]
        identity = OpeningSourceIdentityV1(kind=PAYMENT_KIND, key=pid)
        paid_at = _as_utc(payment["payment_date"])  # type: ignore[arg-type]
        gross = payment_gross[pid]
        residual = payment_residual[pid]
        reference = str(payment.get("reference") or pid)
        cash_lots.append(
            OpeningCashLotV1(
                lot_id=f"legacy-payment:{pid}",
                source_identity=identity,
                partition=partition,
                source_system=SOURCE_SYSTEM,
                source_settlement_key=pid,
                confirmation=OpeningCashConfirmationV1(
                    owner=SOURCE_SYSTEM,
                    reference=reference,
                    version=MAPPING_VERSION,
                    evidence_digest=_digest({"payment": dict(payment)}),
                    evidence_kind="legacy_customer_payment",
                    cash_amount_minor=gross,
                    currency=partition.currency,
                    precision=precision,
                    economic_at=paid_at,
                    recorded_at=paid_at,
                ),
                gross_minor=gross,
                residual_minor=residual,
                consumptions=tuple(
                    OpeningConsumptionV1(
                        reference=f"legacy-alloc:{pid}:{a['invoice_id']}",
                        kind="allocation",
                        amount_minor=_minor(Decimal(str(a["amount_allocated"])), precision) or 0,
                        economic_at=_as_utc(a["allocated_at"]),  # type: ignore[arg-type]
                        recorded_at=_as_utc(a["allocated_at"]),  # type: ignore[arg-type]
                        target_ref=str(a["invoice_id"]),
                        ledger_ref=reference,
                        reverses_ref=None,
                    )
                    for a in sorted(allocs_by_payment.get(pid, []), key=lambda a: str(a["invoice_id"]))
                ),
            )
        )
        dispositions.append(OpeningDispositionV1(identity, partition, "eligible", "translated", residual))

    lots_residual = sum(lot.residual_minor for lot in cash_lots)
    # Rebuild (documents - payments), receivable-positive: item totals less what was discharged by payments,
    # less the unallocated cash credit_notes as lots (net of credit), as the E4 equation expects.
    item_totals = sum(totals[k] for k in item_ids)
    discharged_to_items = sum(d.amount_minor for item in items for d in item.historical_discharges)
    result.rebuild_minor = item_totals - discharged_to_items - lots_residual
    result.stored_balance_minor = stored_total
    result.items = tuple(items)
    result.cash_lots = tuple(cash_lots)
    result.dispositions = tuple(dispositions)
    result.variances = tuple(variances)
    result.origin_residual_minor = origin_residual
    result.census = {
        "invoices": len(invoices),
        "payments": len(payments),
        "items": len(items),
        "lots": len(cash_lots),
        "closed_invoices": len(closed),
        "ignored_payments": len(ignored),
        "unapplied_credit_notes": len(credit_notes),
        "carryable": True,
    }
    return result


def build_complete_manifest(
    *,
    tenant_id: object,
    capture: OpeningCaptureV1,
    translations: Sequence[OpeningTranslation],
) -> OpeningManifestV2:
    """Assemble the ONE complete V2 manifest over every CARRYABLE partition.

    A non-carryable translation is refused here (a12 refuses the whole manifest on one blocking row); the
    census excludes those partitions before this point and records them as E35.
    """

    if not translations:
        raise OpeningTranslationError("cannot build a manifest with no translated partitions")
    ordered = sorted(translations, key=lambda t: (t.partition.source_account_key, t.partition.currency))  # type: ignore[union-attr]
    seen: set[tuple[str, str]] = set()
    dispositions: list[OpeningDispositionV1] = []
    controls: list[OpeningLaneControlsV1] = []
    measures: list[OpeningPartitionMeasuresV1] = []
    receivables: list[OpeningReceivableItemV1] = []
    cash_lots: list[OpeningCashLotV1] = []
    variances: list[OpeningVarianceV1] = []
    for translation in ordered:
        partition = translation.partition
        if partition is None or not translation.carryable:
            raise OpeningTranslationError("only carryable partitions enter the manifest (exclude the rest first)")
        coordinate = (partition.source_account_key, partition.currency)
        if coordinate in seen:
            raise OpeningTranslationError(f"duplicate partition {coordinate}")
        seen.add(coordinate)
        lots_residual = sum(lot.residual_minor for lot in translation.cash_lots)
        controls.append(
            OpeningLaneControlsV1(
                partition=partition,
                collectible_receivable_minor=translation.open_ar_minor,
                available_cash_credit_minor=lots_residual,
                available_non_cash_credit_minor=0,
                unapplied_customer_credit_minor=0,
                prepaid_funding_reserved_minor=0,
                prepaid_funding_consumed_minor=0,
                written_off_total_minor=0,
                refunded_total_minor=0,
                adjustment_total_minor=0,
                open_ar_minor=translation.open_ar_minor,
            )
        )
        rows = len(translation.dispositions)
        measures.append(
            OpeningPartitionMeasuresV1(
                partition=partition,
                document_residual_minor=translation.open_ar_minor,
                stored_balance_minor=translation.stored_balance_minor,
                rebuild_minor=translation.rebuild_minor,
                rebuild_gap_reason=None,
                population_rows=rows,
                transferred_rows=rows,
                carried_rows=0,
                excluded_rows=0,
                archived_rows=0,
                non_carryable_rows=0,
            )
        )
        dispositions.extend(translation.dispositions)
        receivables.extend(translation.items)
        cash_lots.extend(translation.cash_lots)
        variances.extend(translation.variances)
    population = tuple(d.identity for d in dispositions)
    return OpeningManifestV2(
        scope=TenantScope(tenant_id),  # type: ignore[arg-type]
        capture=capture,
        population=population,
        dispositions=tuple(dispositions),
        counts=OpeningPopulationCountsV1(population=len(population), eligible=len(population), excluded=0, blocking=0),
        controls=tuple(controls),
        cash_lots=tuple(cash_lots),
        receivables=tuple(receivables),
        measures=tuple(measures),
        variances=tuple(variances),
        batch_controls=(),
    )


def build_manifest(
    *,
    tenant_id: object,
    capture: OpeningCaptureV1,
    translation: OpeningTranslation,
) -> OpeningManifestV2:
    """One carryable partition as a complete (single-partition) manifest."""

    return build_complete_manifest(tenant_id=tenant_id, capture=capture, translations=(translation,))


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
    partition: OpeningPartitionV1 | None = None,
) -> ImportOpeningAccountV2:
    """Assemble the V2 command after local target/partition agreement.

    `target` is read from the destination at rehearsal (the module's
    `get_opening_cash_target`); `migration_authority_ref` is the
    named approval reference. Neither is derivable here, and neither
    is ever defaulted: a mismatch between target, partition and
    manifest scope is a structural refusal, not a per-row blocker.
    """

    if partition is None:
        # Single-partition manifest convenience only; a complete manifest needs the explicit selection.
        known = {c.partition for c in manifest.controls}
        if len(known) != 1:
            raise OpeningTranslationError("a multi-partition manifest needs the partition to select")
        (partition,) = known
    elif partition not in {c.partition for c in manifest.controls}:
        raise OpeningTranslationError("selected partition is not in the manifest")
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
    "CURRENCY_REASON",
    "CUSTOMER_SNAPSHOT_REASON",
    "INEXACT_REASON",
    "INVOICE_KIND",
    "INVOICE_REQUIRED_FIELDS",
    "MAPPING_VERSION",
    "ORIGINS",
    "OUTSIDE_POPULATION_REASON",
    "OVERALLOCATED_REASON",
    "PAYMENT_KIND",
    "PAYMENT_OVERALLOCATED_REASON",
    "PAYMENT_REQUIRED_FIELDS",
    "SOURCE_SYSTEM",
    "STORED_VARIANCE_REASON",
    "SUB_ORIGIN_FIELD",
    "UNAPPLIED_CREDIT_REASON",
    "OpeningBlocker",
    "OpeningPreflight",
    "OpeningTranslation",
    "OpeningTranslationContext",
    "OpeningTranslationError",
    "OpeningUnappliedCreditNote",
    "build_complete_manifest",
    "build_import_command",
    "build_manifest",
    "origin_of",
    "preflight_opening_import",
    "translate_partition",
]
