"""Test helper: drive the real billing commands (a11 lane) for the B-15 PG tests. No mocks of billing."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from dotmac_billing.commands import (
    AllocationCommand,
    CreateDraftDocument,
    DeallocationCommand,
    IssueDocument,
    RefundCommand,
    ReversePostingGroupCommand,
)
from dotmac_billing.contracts import (
    AcceptRatedObligationV1,
    AcceptSettlementV1,
    AppliedFxSnapshotV1,
    AppliedTaxSnapshotV1,
    DueDateBasisStatus,
    DueDateBasisV1,
    PartyDocumentSnapshotV1,
    PartyTaxIdentitySnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
    PresentationAssetReferenceV1,
    ServicePeriodEvidenceV1,
    ServicePeriodStatus,
    SettlementFundingLane,
)
from dotmac_billing.models import AllocationEffect, PostingEffect
from dotmac_billing.service import (
    accept_rated_obligation,
    accept_settlement,
    allocate_settlement,
    create_billing_account,
    create_draft_document,
    deallocate_settlement,
    issue_document,
    refund_settlement,
    reverse_posting_group,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from sqlalchemy import func, select

BASE = datetime(2026, 1, 10, 9, tzinfo=UTC)
PERIOD_START = datetime(2026, 1, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 2, 1, tzinfo=UTC)


class Numbering:
    def allocate(self, _db, *, scope, series_code, reference_date, idempotency_key, document_reference):
        return f"{series_code.upper()}-{uuid4().hex[:12]}"


def money(amount, currency="NGN") -> Money:
    return Money.of(str(amount), Currency(currency, 2))


def at(hours: int) -> datetime:
    return BASE + timedelta(hours=hours)


def _basis(when=BASE) -> DueDateBasisV1:
    return DueDateBasisV1(
        status=DueDateBasisStatus.VERIFIED,
        source_authority="contract.v1",
        evidence_ref="contract:line:1:v1",
        payment_terms_code="net_30",
        payment_terms_version="1",
        issued_at=when,
        effective_at=when,
        timezone="Africa/Lagos",
        derivation_policy="calendar_days",
        derivation_version="1",
    )


def _party(name):
    return PartyDocumentSnapshotV1(
        legal_name=name, address=PostalAddressSnapshotV1(line_one="Plot 1", city="Abuja", country_code="NGA")
    )


def new_account(db, scope, currency="NGN"):
    return create_billing_account(
        db, scope=scope, external_account_ref=f"customer-{uuid4().hex[:10]}", currency=currency, minor_units=2
    )


def new_invoice(db, scope, account, *, total="120.00", when=BASE, currency="NGN"):
    obligation = accept_rated_obligation(
        db,
        scope=scope,
        command=AcceptRatedObligationV1(
            scope=scope,
            billing_account_id=account.id,
            contract_line_ref="contract-line-1",
            contract_version="v1",
            charge_component="recurring_access",
            source_system="subscriptions",
            source_kind="recurring_charge_occurrence",
            source_fact_id=f"occ-{uuid4().hex}",
            source_fact_version="1",
            subject_ref="customer:example",
            service_ref="service:example",
            service_period=ServicePeriodEvidenceV1(
                status=ServicePeriodStatus.VERIFIED, starts_at=PERIOD_START, ends_at=PERIOD_END
            ),
            collection_timing="arrears",
            pre_tax_amount=money("100.00", currency),
            tax_amount=money("20.00", currency),
            total_amount=money(total, currency),
            rated_at=when,
            price_version_id="price:v1",
            tax_snapshots=(
                AppliedTaxSnapshotV1(
                    treatment_code="standard",
                    jurisdiction_code="zone",
                    policy_id="tax-policy",
                    policy_version="1",
                    rate=Decimal("0.200000"),
                    taxable_basis=money("100.00", currency),
                    tax_amount=money("20.00", currency),
                ),
            ),
            fx_snapshot=AppliedFxSnapshotV1(
                observation_id="fx-1",
                observation_version="1",
                base_currency=currency,
                quote_currency=currency,
                rate=Decimal("1.000000"),
                rate_purpose="document_currency",
                observed_at=when,
                effective_at=when,
                rounding_policy="minor_units",
                provenance="source-supplied",
            ),
        ),
        accepted_source_kinds=frozenset({"recurring_charge_occurrence"}),
    )
    draft = create_draft_document(
        db,
        scope=scope,
        command=CreateDraftDocument(
            obligation_id=obligation.id,
            description="Monthly access",
            quantity=Decimal("1.000000"),
            unit_code="month",
            seller_snapshot=_party("Dotmac Technologies Ltd"),
            customer_snapshot=_party("Customer Ltd"),
            payment_instructions=PaymentInstructionsSnapshotV1(
                method_code="bank_transfer",
                bank_name="Bank",
                account_name="Dotmac",
                account_reference="ref",
            ),
            brand_asset=PresentationAssetReferenceV1.none(),
            locale="en-NG",
            timezone="Africa/Lagos",
            document_profile_code="invoice.standard",
            document_profile_version="1",
            due_date_basis=_basis(when),
            party_tax_identities=(
                PartyTaxIdentitySnapshotV1(
                    party_role="seller",
                    identity_type="tax_id",
                    identity_value="v",
                    country_code="NGA",
                    source_authority="party.v1",
                    source_version="1",
                ),
            ),
        ),
    )
    return issue_document(
        db,
        scope=scope,
        command=IssueDocument(
            document_id=draft.id,
            series_code="invoice",
            reference_date=date(2026, 1, 10),
            due_at=when + timedelta(days=30),
            due_date_basis=_basis(when),
            actor_ref="billing-test",
            correlation_id=f"issue:{draft.id}",
        ),
        numbering=Numbering(),
    )


def receipt(db, scope, account, amount, *, when, lane=SettlementFundingLane.AVAILABLE_CREDIT, source="integrator"):
    return accept_settlement(
        db,
        scope=scope,
        command=AcceptSettlementV1(
            scope=scope,
            billing_account_id=account.id,
            source_system=source,
            source_settlement_key=f"s-{uuid4().hex}",
            source_version="1",
            amount=money(amount),
            occurred_at=when,
            observed_at=when,
            confirmation_evidence="provider_settled",
            funding_lane=lane,
        ),
        accepted_confirmation_evidence=frozenset({"provider_settled"}),
    )


def allocate(db, scope, settlement, invoice, amount, *, when):
    return allocate_settlement(
        db,
        scope=scope,
        command=AllocationCommand(settlement.id, invoice.id, money(amount), when, f"alloc-{uuid4().hex}"),
    )


def deallocate(db, scope, group, amount, *, when):
    row = db.execute(select(AllocationEffect).where(AllocationEffect.posting_group_id == group.id)).scalar_one()
    return deallocate_settlement(
        db, scope=scope, command=DeallocationCommand(row.id, money(amount), when, f"dealloc-{uuid4().hex}")
    )


def refund(db, scope, settlement, amount, *, when):
    return refund_settlement(
        db, scope=scope, command=RefundCommand(settlement.id, money(amount), when, f"refund-{uuid4().hex}")
    )


def reverse(db, scope, group, *, when):
    return reverse_posting_group(
        db, scope=scope, command=ReversePostingGroupCommand(group.id, when, f"rev-{uuid4().hex}")
    )


def positions(db, tenant_id: UUID, account_ids, currency="NGN") -> dict[str, Decimal]:
    """Billing's own lane positions: the sum of its immutable posting effects (the T2 tie reference)."""
    rows = db.execute(
        select(PostingEffect.lane, func.coalesce(func.sum(PostingEffect.amount_delta), 0))
        .where(
            PostingEffect.tenant_id == tenant_id,
            PostingEffect.billing_account_id.in_(list(account_ids)),
            PostingEffect.currency == currency,
        )
        .group_by(PostingEffect.lane)
    ).all()
    out = {"receivable": Decimal(0), "available_credit": Decimal(0), "prepaid_funding": Decimal(0)}
    out.update({lane: Decimal(total) for lane, total in rows})
    return out


__all__ = ["TenantScope"]


def credit_note(db, scope, original, *, pre_tax="50.00", tax="10.00", when, currency="NGN"):
    """A credit note against one issued invoice (the original's id; the lines are the caller's)."""
    from dotmac_billing.commands import IssueCreditNote
    from dotmac_billing.service import issue_credit_note

    total = Decimal(pre_tax) + Decimal(tax)
    return issue_credit_note(
        db,
        scope=scope,
        command=IssueCreditNote(
            original_document_id=original.id,
            pre_tax_amount=money(pre_tax, currency),
            tax_amount=money(tax, currency),
            total_amount=money(total, currency),
            reason="service credit",
            series_code="credit",
            reference_date=date(2026, 1, 10),
            actor_ref="billing-test",
            correlation_id=f"cn:{uuid4().hex}",
            occurred_at=when,
            document_profile_code="invoice.standard",
            document_profile_version="1",
            tax_snapshots=(
                AppliedTaxSnapshotV1(
                    treatment_code="standard",
                    jurisdiction_code="zone",
                    policy_id="tax-policy",
                    policy_version="1",
                    rate=Decimal("0.200000"),
                    taxable_basis=money(pre_tax, currency),
                    tax_amount=money(tax, currency),
                ),
            ),
        ),
        numbering=Numbering(),
    )


def void(db, scope, document, *, when):
    from dotmac_billing.service import void_document

    return void_document(
        db,
        scope=scope,
        document_id=document.id,
        actor_ref="billing-test",
        occurred_at=when,
        source_ref=f"void-{uuid4().hex}",
    )
