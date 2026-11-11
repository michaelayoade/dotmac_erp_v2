"""Waves 3-6 pairings: finance core, revenue, inventory/projects, satellites.

Same contract as wave2.py. Two destinations classes deserve emphasis:

- **Billing** is a fact store, not an invoice table set: legacy AR
  enters through its opening-window commands (open receivables,
  opening cash, historical discharges) per the billing cutover
  rulings, never as row copies. Every tenant billing table is
  therefore declared GENERATED here; the wave-4 work is command
  translation + parity, after the Starter billing gap plan (Sub M1).
- **Derived balances/projections** (inventory stock_balances,
  analytics points, payables obligations) are rebuilt by their
  module from imported movements/facts, never imported directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from erp_v2.importer.spec import SourceRowRefusal, TransformSpec, build_spec, destination_columns_from_table
from erp_v2.importer.sub_origin import SubCustomerRef, SubOriginPolicy
from erp_v2.importer.wave2 import Pairing

_BILLING_TENANT_TABLES: Final = (
    "settlement_earmarks",
    "settlement_earmark_targets",
    "settlement_earmark_effects",
    "opening_windows",
    "opening_window_admissions",
    "opening_window_seals",
    "receivable_source_claims",
    "opening_receivable_bases",
    "opening_historical_discharge_claims",
    "receivable_exposure_facts_v2",
    "opening_import_receipts",
    "opening_cash_lots",
    "opening_cash_effects",
    "cash_source_claims",
    "non_cash_credit_grants",
    "non_cash_credit_effects",
    "receivable_position_facts_v2",
    "withholding_discharges",
    "billing_accounts",
    "rated_obligations",
    "documents",
    "document_lines",
    "document_events",
    "confirmed_settlements",
    "posting_groups",
    "posting_effects",
    "allocation_effects",
    "applied_tax_snapshots",
    "applied_fx_snapshots",
    "party_tax_identity_snapshots",
    "invoice_document_facts",
    "document_artifacts",
    "accounting_facts",
    "receivable_position_facts",
    "receivable_exposure_facts",
)


def _generated(schema: str, tables: Sequence[str], note: str) -> tuple[Pairing, ...]:
    return tuple(Pairing(f"{schema}.{t}", None, note=note) for t in tables)


WAVE3_PAIRINGS: Final[tuple[Pairing, ...]] = (
    # accounting (mod_accounting) - history enters as typed facts;
    # posted lines are immutable ledger evidence.
    Pairing(
        "mod_accounting.accounts",
        "public.account",
        renames={"posting_allowed": "is_posting_allowed"},
        note="legacy CONTROL/STATISTICAL accounts with posting allowed "
        "violate the module invariant - named exceptions (16 live)",
    ),
    Pairing(
        "mod_accounting.account_categories",
        "public.account_category",
        renames={"account_class": "ifrs_category"},
        fk_rewrites={"account_class": "account_class_by_legacy"},
    ),
    Pairing("mod_accounting.fiscal_years", "public.fiscal_year"),
    Pairing(
        "mod_accounting.fiscal_periods",
        "public.fiscal_period",
        constants={"status": "FUTURE"},
        note="lifecycle-staged: module trigger requires inserts to begin "
        "FUTURE; transitions step walks OPEN/SOFT_CLOSED after import",
    ),
    Pairing(
        "mod_accounting.accounting_dimensions",
        "public.coa_segment_definition",
        renames={"id": "segment_def_id", "code": "segment_type", "name": "segment_name"},
    ),
    Pairing(
        "mod_accounting.accounting_dimension_values",
        "public.coa_segment_value",
        renames={"dimension_id": "segment_def_id", "code": "segment_code", "name": "segment_name"},
    ),
    Pairing(
        "mod_accounting.journal_entries",
        "public.journal_entry",
        renames={
            "number": "journal_number",
            "kind": "journal_type",
            "source_document_kind": "source_document_type",
            "posted_by": "posted_by_user_id",
            "reverses_journal_id": "reversed_journal_id",
        },
        constants={"status": "DRAFT"},
        computed={"source_document_id": "source_key"},
        deferred=("reverses_journal_id", "reversal_journal_id"),
        null_fallbacks={"source_document_kind": "literal:JOURNAL"},
        note="lifecycle-staged: journals must be assembled DRAFT (lines "
        "immutable after); transitions step posts/voids/reverses after "
        "lines + posted ledger land. Legacy approval_request_id is "
        "empty in production data - migrated journals need no "
        "approval provenance (ruling 2026-11-09): the posting stamp "
        "is the legacy journal ID + posting date, receipt-counted. "
        "source_document_id: legacy records none for 48% of journals "
        "and duplicates (type, id) across 4,289 documents, so it is "
        "not a posting identity; the legacy journal row itself is "
        "the source document of record (source_key) and the legacy "
        "source_document_id stays residue evidence",
    ),
    Pairing(
        "mod_accounting.journal_lines",
        "public.journal_entry_line",
        renames={
            "journal_id": "journal_entry_id",
            "debit": "debit_amount",
            "credit": "credit_amount",
            "debit_functional": "debit_amount_functional",
            "credit_functional": "credit_amount_functional",
        },
        note="dimension columns (business_unit/cost_center/project/segment) split into journal_line_dimensions",
    ),
    Pairing(
        "mod_accounting.posted_ledger_lines",
        "public.posted_ledger_line",
        renames={
            "journal_id": "journal_entry_id",
            "currency_code": "original_currency_code",
            "debit": "debit_amount",
            "credit": "credit_amount",
            "original_debit": "original_debit_amount",
            "original_credit": "original_credit_amount",
            "source_document_kind": "source_document_type",
            "posted_by": "posted_by_user_id",
        },
        null_fallbacks={
            "source_document_id": "row_id",
            "source_document_kind": "literal:JOURNAL",
            "currency_code": "literal:NGN",
            "original_debit": "column:debit",
            "original_credit": "column:credit",
            "exchange_rate": "literal:1",
        },
        fk_rewrites={"journal_line_id": "journal_line"},
        lookups={"journal_number": ("journal_number_by_entry", "journal_id", "journal_number")},
        note="journal_line_id rewrites through the journal_line map: "
        "identity for real entry lines; 9 production ledger rows "
        "carry phantom line ids (payroll JE0001/JE0002) that resolve "
        "only to the unique same-journal/account/amount entry line, "
        "else named exceptions. "
        "167k legacy ledger lines are functional-only postings "
        "(no original leg recorded); every journal is NGN at rate 1 "
        "in production data, so original falls back to functional "
        "in NGN - the POSTED transition trigger verifies the sums "
        "against entry totals either way",
    ),
    *_generated(
        "mod_accounting",
        ("journal_line_dimensions", "posted_ledger_dimensions", "period_events"),
        "GENERATED: dimension rows split from legacy line columns; period events from fiscal period state",
    ),
    # banking (mod_banking)
    Pairing(
        "mod_banking.bank_institutions",
        "public.bank_directory",
        renames={"code": "bank_code", "name": "bank_name"},
        constants={"status": "active", "country_code": "NG"},
        computed={"id": "uuid5_row"},
        note="legacy directory is the Nigerian NIBSS bank list (nibss_code); country fixed NG",
    ),
    Pairing(
        "mod_banking.bank_accounts",
        "public.bank_accounts",
        renames={
            "institution_id": "bank_account_id",
            "account_code": "account_number",
            "account_identifier": "account_number",
            "account_type_code": "account_type",
            "cash_account_ref": "gl_account_id",
        },
        fk_rewrites={"institution_id": "bank_institution_by_account"},
        note="institution resolves per account (CBN code, else name; "
        "fintech institutions mint from legacy accounts first); "
        "mono_* sync state is adapter residue",
    ),
    Pairing(
        "mod_banking.bank_statements",
        "public.bank_statements",
        renames={"account_id": "bank_account_id", "imported_by_id": "imported_by"},
        null_fallbacks={"imported_by_id": "run:imported_by_id"},
    ),
    Pairing(
        "mod_banking.bank_statement_lines",
        "public.bank_statement_lines",
        renames={"direction": "transaction_type", "external_ref": "bank_reference"},
        fk_rewrites={"direction": "bank_direction_by_type"},
        null_fallbacks={"external_ref": "source_key"},
        note="direction: legacy transaction_type value-mapped to the module "
        "check values (credit/debit) via the enrolment map; unmapped types "
        "are exceptions. Categorization suggestions are residue (re-derived). "
        "Traditional-bank statement lines carry no bank reference (all "
        "18,187 lines of landed statements) - external_ref falls back "
        "to the provenance source_key (Gate 5 ruling family)",
    ),
    Pairing(
        "mod_banking.match_allocations",
        None,
        note="GENERATED: allocations are outputs of match decisions over "
        "cash observations (both GENERATED); legacy bank_statement_line_matches "
        "rows are rehearsal evidence for re-derivation, never row copies - "
        "they carry neither decision nor observation ids",
    ),
    Pairing(
        "mod_banking.reconciliations",
        None,
        note="GENERATED: a module reconciliation binds one statement snapshot; "
        "legacy bank_reconciliations are per-account period reports with no "
        "statement link and ambiguous total_* semantics - recomputed per "
        "statement at enrolment, legacy rows are parity evidence",
    ),
    *_generated(
        "mod_banking",
        ("cash_account_observations", "match_policies", "match_decisions"),
        "GENERATED: observations arrive via Integrator/adapters; policies are tenant config at enrolment",
    ),
    # payables (mod_payables)
    Pairing(
        "mod_payables.supplier_invoices",
        "public.supplier_invoice",
        renames={
            "number": "invoice_number",
            "supplier_document_number": "supplier_invoice_number",
            "liability_account_ref": "ap_control_account_id",
            "submitted_by": "submitted_by_user_id",
            "approved_by": "approved_by_user_id",
        },
        constants={"status": "DRAFT"},
        lookups={
            "supplier_name_snapshot": ("supplier", "supplier_ref", "name"),
            "payment_schedule": ("supplier_payment_schedule", "id", "payment_schedule"),
        },
        note="payment_schedule: single-instalment JSON assembled at enrolment "
        "from due_date + total (module schedule shape, sums to the total); "
        "invoices without a due date exception. Lifecycle-staged DRAFT; "
        "transitions step walks SUBMITTED/APPROVED/VOID after lines land",
    ),
    Pairing(
        "mod_payables.supplier_invoice_lines",
        "public.supplier_invoice_line",
        renames={"posting_account_ref": "expense_account_id"},
        lookups={"dimension_refs": ("supplier_line_dimensions", "id", "dimension_refs")},
        note="dimension_refs: enrolment-assembled sorted [CODE, value] JSON "
        "from cost_center_id/project_id/segment_id (declared codes "
        "COST_CENTER/PROJECT/SEGMENT)",
    ),
    Pairing(
        "mod_payables.settlement_observations",
        "public.supplier_payment",
        source_row_refusal=SourceRowRefusal(
            source_key="payment_id",
            reason="historical_ap_payment_not_settlement_evidence",
        ),
        note="Retain original supplier payment evidence in the source snapshot; "
        "legacy payment states and allocations do not establish bank settlement. "
        "Every source row is a named refusal, independent of destination obligations.",
    ),
    *_generated(
        "mod_payables",
        (
            "credit_notes",
            "credit_note_lines",
            "payment_obligations",
            "liability_events",
            "credit_applications",
            "accounting_receipts",
        ),
        "GENERATED/derived by payables service from imported invoices; "
        "legacy credit notes (invoice type) split at rehearsal",
    ),
    # payments (mod_payments)
    Pairing(
        "mod_payments.payment_intents",
        "public.payment_intent",
        renames={
            "reference": "paystack_reference",
            "requested_amount": "amount",
            "opened_at": "created_at",
            "settled_at": "paid_at",
        },
        constants={"provider_type": "paystack"},
        lookups={
            "payer_reference": ("payment_intent_terms", "id", "payer_reference"),
            "purpose": ("payment_intent_terms", "id", "purpose"),
            "channel": ("payment_intent_terms", "id", "channel"),
        },
        note="terms derive at enrolment where evidence is exact: INBOUND "
        "payer = customer email, INVOICE source = INVOICE_SETTLEMENT "
        "purpose, channel from the gateway response; EXPENSE_CLAIM/"
        "GENERAL intents have no exact module purpose and exception",
    ),
    Pairing(
        "mod_payments.payment_confirmations",
        "public.customer_payment",
        source_row_refusal=SourceRowRefusal(
            source_key="payment_id",
            reason="historical_customer_payment_not_confirmation_evidence",
        ),
        sub_origin=SubOriginPolicy.EXCLUDE,  # F1: Sub-origin cash is Sub Billing's, never ERP v2's
        note="Retain original customer payment evidence in the frozen source snapshot; "
        "native rows are named refusals, never confirmed cash. Sub-tagged rows "
        "remain excluded under the approved Sub-origin policy.",
    ),
    *_generated(
        "mod_payments",
        (
            "payment_transfer_proofs",
            "merchant_contexts",
            "merchant_context_ends",
            "provider_cash_qualifications",
        ),
        "GENERATED: merchant context is enrolment config; proofs/qualifications are provider facts post-flip",
    ),
    # expenses (mod_expenses)
    Pairing("mod_expenses.expense_categories", "public.expense_category"),
    Pairing(
        "mod_expenses.expense_claims",
        "public.expense_claim",
        renames={"reference": "claim_number", "claimant_party_id": "employee_id"},
        fk_rewrites={"claimant_party_id": "employee_party"},
        note="claimant is a Party id: rewritten through the employee->party map from the people import",
    ),
    Pairing("mod_expenses.expense_claim_lines", "public.expense_claim_item"),
    Pairing(
        "mod_expenses.expense_policies",
        "public.expense_limit_rule",
        constants={"version": 1},
        note="legacy rules enrol as policy v1",
    ),
    Pairing(
        "mod_expenses.expense_policy_evaluations",
        "public.expense_limit_evaluation",
        renames={"actual_amount": "claim_amount", "reason_code": "result"},
        lookups={"batch_id": ("expense_evaluation_batch", "claim_id", "batch_id")},
        note="batch_id: deterministic per-claim batch from the enrolment "
        "map (legacy evaluations are unbatched evidence)",
    ),
    Pairing(
        "mod_expenses.expense_lifecycle_events",
        "public.expense_claim_action",
        renames={"to_status": "status", "occurred_at": "created_at"},
        note="actor_reference stays MISSING: legacy actions record no actor "
        "column; enrolment rules system-actor attribution or exceptions "
        "per action_type at rehearsal - never invented here",
    ),
    Pairing(
        "mod_expenses.expense_requests",
        "public.expense_entry",
        renames={
            "id": "expense_id",
            "reference": "expense_number",
            "requester_party_id": "created_by",
            "purpose": "description",
            "total_requested_amount": "amount",
        },
        fk_rewrites={"requester_party_id": "user_party"},
        note="needed_by stays MISSING (legacy expense_date is when the "
        "expense was incurred, not a need-by date); requester rewritten "
        "through the user->party map from identity enrolment",
    ),
    *_generated(
        "mod_expenses",
        ("expense_request_lines", "expense_policy_rules", "expense_receipts"),
        "GENERATED/split at rehearsal: lines and rules derive from legacy rows; receipts reference files-module ids",
    ),
    # procurement (mod_procurement)
    Pairing(
        "mod_procurement.purchase_requisitions",
        "public.purchase_requisition",
        renames={"requested_on": "requisition_date"},
        note="legacy requisition has no organization column: tenant comes from the run extent",
    ),
    Pairing(
        "mod_procurement.purchase_requisition_lines",
        "public.purchase_requisition_line",
        renames={
            "unit": "uom",
            "estimated_unit_cost": "estimated_unit_price",
            "estimated_total": "estimated_amount",
        },
    ),
    Pairing(
        "mod_procurement.sourcing_events",
        "public.request_for_quotation",
        renames={
            "id": "rfq_id",
            "event_number": "rfq_number",
            "method": "procurement_method",
            "opens_at": "rfq_date",
            "closes_at": "closing_date",
            "criteria_json": "evaluation_criteria",
            "estimated_amount": "estimated_value",
            "created_by_ref": "created_by_user_id",
        },
        note="tenant from the run extent (legacy RFQ carries no org column)",
    ),
    Pairing(
        "mod_procurement.sourcing_invitations",
        "public.rfq_invitation",
        renames={"event_id": "rfq_id"},
        note="tenant/invited_by from the run extent",
    ),
    Pairing(
        "mod_procurement.bid_submissions",
        "public.quotation_response",
        renames={"event_id": "rfq_id", "source_event_id": "response_id"},
        note="source_event_id is the legacy response's own id in the source "
        "system; content_sha256 computes over the imported row",
    ),
    Pairing(
        "mod_procurement.bid_lines",
        "public.quotation_response_line",
        renames={
            "bid_id": "response_id",
            "sourcing_line_id": "requisition_line_id",
            "line_total": "line_amount",
        },
        fk_rewrites={"sourcing_line_id": "sourcing_line_by_requisition_line"},
        lookups={"event_id": ("bid_submission", "bid_id", "event_id")},
        note="sourcing lines are GENERATED snapshots of requisition lines - "
        "the rewrite map binds them; event via the parent bid lookup",
    ),
    Pairing(
        "mod_procurement.bid_evaluations",
        "public.bid_evaluation",
        renames={
            "event_id": "rfq_id",
            "selected_bid_id": "recommended_response_id",
            "evaluated_at": "evaluation_date",
            "evaluated_by_ref": "evaluated_by_user_id",
        },
        lookups={
            "scores_json": ("bid_scores", "id", "scores_json"),
            "selected_total_score": ("bid_scores", "id", "selected_total_score"),
        },
        note="scores assemble at enrolment from legacy bid_evaluation_score "
        "rows (criterion codes slugified from criterion_name; weighted "
        "totals sum the legacy weighted_score column)",
    ),
    Pairing(
        "mod_procurement.purchase_orders",
        "public.purchase_order",
        renames={
            "id": "po_id",
            "order_number": "po_number",
            "ordered_on": "po_date",
            "terms": "terms_and_conditions",
        },
        note="source requisition/evaluation links resolve via procurement evidence at rehearsal",
    ),
    Pairing(
        "mod_procurement.purchase_order_lines",
        "public.purchase_order_line",
        renames={"order_id": "po_id"},
        lookups={"unit": ("inventory_item", "item_ref", "base_uom")},
        note="unit from the item's base_uom via the enrolment item map; "
        "free-text lines without item_id exception for a unit ruling",
    ),
    Pairing(
        "mod_procurement.receipt_observations",
        "public.goods_receipt",
        renames={
            "order_id": "po_id",
            "source_event_id": "receipt_id",
            "observed_at": "receipt_date",
        },
        lookups={"lines_json": ("receipt_lines", "id", "lines_json")},
        note="lines_json assembled at enrolment from legacy goods_receipt "
        "lines in the module canonical form; payload_sha256 computes "
        "over the imported row including the assembled lines",
    ),
    *_generated(
        "mod_procurement",
        ("sourcing_event_lines", "procurement_evidence"),
        "GENERATED: sourcing lines snapshot from requisition lines; evidence is digest-bound input",
    ),
    # approvals (mod_approvals, tenant plane)
    Pairing(
        "mod_approvals.approval_requests",
        "public.approval_request",
        renames={
            "subject_type": "document_type",
            "subject_id": "document_id",
            "requested_by": "requested_by_user_id",
            "state": "status",
        },
        computed={"idempotency_key": "source_key"},
        lookups={
            "policy_code": ("approval_request_policy", "id", "policy_code"),
            "policy_version": ("approval_request_policy", "id", "policy_version"),
        },
        note="policy_code/version resolve from the legacy workflow via the "
        "approval_request_policy enrolment map (policies enrol as v1); "
        "content_digest computes over the imported row; state values "
        "verified against the module state set at rehearsal",
    ),
    Pairing(
        "mod_approvals.approval_decisions",
        "public.approval_decision",
        renames={"actor_id": "approver_user_id"},
        note="tenant from the run extent (legacy decisions carry no org column)",
    ),
    Pairing(
        "mod_approvals.approval_policies",
        "public.approval_workflow",
        renames={"policy_code": "workflow_code", "levels": "approval_levels"},
        constants={"version": 1},
        note="enrols as policy v1 (re-published through the approvals "
        "service); document_digest computes over the imported row",
    ),
    *_generated(
        "mod_approvals",
        ("approval_withdrawals",),
        "GENERATED post-flip (no legacy withdrawal state)",
    ),
    # finance / fixed assets (mod_finance) + assets (mod_assets)
    Pairing(
        "mod_assets.assets",
        "public.asset",
        renames={
            "code": "asset_number",
            "state": "status",
            "acquired_on": "acquisition_date",
            "tag": "barcode",
        },
        lookups={
            "kind": ("asset_kind", "id", "kind"),
            "condition": ("asset_condition", "id", "condition"),
        },
        note="kind from the legacy category_code; condition from the "
        "latest assignment condition evidence (enum-exact values only, "
        "assets without evidence exception). Valuation columns are "
        "finance-owned (asset books), not asset-registry data",
    ),
    Pairing(
        "mod_assets.asset_assignments",
        "public.asset_assignment",
        renames={"custodian_id": "employee_id", "starts_on": "issued_on"},
        fk_rewrites={"custodian_id": "employee_party"},
        note="custodian rewritten through the employee->party map",
    ),
    Pairing(
        "mod_assets.asset_disposals",
        "public.asset_disposal",
        renames={
            "method": "disposal_type",
            "requested_on": "disposal_date",
            "requested_by_id": "created_by_user_id",
            "reason": "disposal_reason",
            "disposed_on": "disposal_date",
            "approved_by_id": "approved_by_user_id",
            "recipient_ref": "buyer_reference",
            "external_authorization_ref": "authorization_reference",
            "external_finance_ref": "journal_entry_id",
        },
        constants={"status": "completed"},
        note="legacy disposal rows are completed facts (DisposalStatus); "
        "tenant from the run extent (no org column); proceeds/gain-loss "
        "amounts are finance consequences, not registry columns",
    ),
    Pairing(
        "mod_assets.asset_lifecycle_events",
        "public.asset_lifecycle_event",
        renames={"occurred_at": "event_at"},
    ),
    Pairing(
        "mod_assets.asset_maintenance",
        None,
        note="GENERATED post-flip: legacy asset_tracking_event rows are "
        "GPS/scan tracking observations (latitude/longitude/tracked_at), "
        "not maintenance records - they stay lifecycle/fleet evidence and "
        "must not be copied into maintenance. Corrected 2026-11-08",
    ),
    Pairing(
        "mod_finance.depreciation_runs",
        "public.depreciation_run",
        renames={
            "run_ref": "run_number",
            "period_ref": "fiscal_period_id",
            "calculated_at": "calculation_completed_at",
        },
        lookups={
            "through_date": ("fiscal_period", "period_ref", "end_date"),
            "currency_code": ("tenant_currency", "tenant_id", "currency_code"),
        },
        note="currency from the legacy organisation functional currency via the tenant_currency enrolment map",
    ),
    Pairing(
        "mod_finance.depreciation_lines",
        None,
        note="GENERATED: module lines are computed from enrolled asset "
        "books incl. the unimpaired-carrying shadow legacy never stored; "
        "legacy depreciation_schedule rows are run-parity evidence "
        "(amount equality per run), never row copies",
    ),
    *_generated(
        "mod_finance",
        ("asset_books", "accounting_events", "accounting_consequences", "accounting_consequence_lines"),
        "GENERATED: books enrol per asset; consequences are balanced outputs "
        "of impairment/revaluation/depreciation events",
    ),
)

WAVE4_PAIRINGS: Final[tuple[Pairing, ...]] = (
    *_generated(
        "mod_billing",
        _BILLING_TENANT_TABLES,
        "COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via "
        "opening-window commands (opening receivables/cash/discharges), never row copies; "
        "open items only per billing M8; after Starter billing gap plan (Sub M1 first)",
    ),
    Pairing(
        "mod_customers.customer_accounts",
        "public.customer",
        renames={"account_number": "customer_code", "display_name": "legal_name"},
        sub_origin=SubOriginPolicy.EXCLUDE,  # pending Michael's M11 ruling on Sub customers
        note="status from is_active/credit_hold at rehearsal; credit/tax/banking detail lands in customer_profiles",
    ),
    Pairing(
        "mod_customers.customer_profiles",
        "public.customer",
        renames={"account_id": "customer_id"},
        sub_origin=SubOriginPolicy.EXCLUDE,  # pending Michael's M11 ruling on Sub customers
        note="second spec from the same legacy row (profile columns split); "
        "account_id is the preserved customer UUID (customer_accounts.id)",
    ),
    *_generated(
        "mod_customers",
        ("customer_party_references",),
        "GENERATED: party link resolved against kernel Party import",
    ),
    Pairing(
        "mod_agreements.agreements",
        "public.contract",
        renames={
            "reference": "contract_number",
            "counterparty_ref": "customer_id",
            "agreement_type": "contract_type",
            "effective_date": "start_date",
            "expiry_date": "end_date",
        },
        sub_customer_ref=SubCustomerRef("customer_id"),  # counterparty_ref may not be an FK
        lookups={"agreement_family_id": ("agreement_family", "id", "family_id")},
        note="commercial contracts only; employment/lease/performance "
        "contracts are e2. agreement_family_id: deterministic family per "
        "contract chain from the agreement_family enrolment map "
        "(amendment lineage via original_contract_id evidence)",
    ),
    Pairing(
        "mod_agreements.agreement_events",
        None,
        note="GENERATED post-flip: legacy contract_amendment_workflow rows "
        "are HR sign-off chains (appraisee/appraiser/hod/hr_head columns - "
        "e2 appraisals evidence), not agreement events; copying them here "
        "would fabricate agreement history. Corrected 2026-11-08",
    ),
    *_generated(
        "mod_agreements",
        ("agreement_lines", "agreement_approval_withdrawals"),
        "GENERATED/split at rehearsal",
    ),
    Pairing(
        "mod_sales.quotes",
        "public.quote",
        renames={
            "currency": "currency_code",
            "tax_total": "tax_amount",
            "total": "total_amount",
            "expires_at": "valid_until",
            "notes": "customer_notes",
            "authored_by_opaque_id": "created_by",
        },
        constants={"authored_by_kind": "user"},
        sub_customer_ref=SubCustomerRef("customer_id"),
        lookups={
            "lead_id": ("quote_lead", "id", "lead_id"),
            "authored_by_label": ("user_label", "authored_by_opaque_id", "label"),
            "tax_rate": ("quote_tax_rate", "id", "tax_rate"),
        },
        note="lead_id resolves to the enrolment-minted per-customer lead; "
        "authored_by_kind is the declared enrolment value for legacy user "
        "authors, label from the legacy credential username. tax_rate "
        "resolves only for single-rate quotes (legacy fraction x100 to "
        "the module percent semantics)",
    ),
    Pairing(
        "mod_sales.quote_lines",
        "public.quote_line",
        renames={"position": "line_number", "amount": "line_total"},
        computed={"gross_amount": "mul:quantity:unit_price"},
        sub_customer_ref=SubCustomerRef("quote_id", via_table="quote", via_key="quote_id", via_customer="customer_id"),
        note="gross_amount computes as quantity x unit_price (exact "
        "Decimal); rehearsal parity-checks amount+discount against it",
    ),
    *_generated(
        "mod_sales",
        ("pipelines", "pipeline_stages", "leads", "lead_origins", "quote_discount_revisions"),
        "GENERATED: pipeline is enrolment config; no legacy lead estate in ERP",
    ),
    *_generated(
        "mod_coll",
        (
            "collection_policies",
            "collection_policy_versions",
            "collection_policy_steps",
            "collection_cases",
            "collection_case_exposures",
            "collection_case_transitions",
            "collection_step_attempts",
            "payment_arrangements",
            "payment_arrangement_exposures",
            "payment_arrangement_installments",
            "payment_arrangement_settlement_receipts",
            "collection_grace_grants",
            "collection_notice_requests",
            "collection_notice_receipts",
            "collection_action_requests",
            "collection_action_receipts",
            "collection_reconciliations",
        ),
        "GENERATED: cases open post-flip from billing exposures; policy is enrolment config",
    ),
    *_generated(
        "mod_party",
        (
            "party_roles",
            "party_relationships",
            "party_memberships",
            "party_contact_points",
            "party_external_references",
        ),
        "GENERATED at identity enrolment from kernel Party + employee/customer/supplier links",
    ),
)

WAVE56_PAIRINGS: Final[tuple[Pairing, ...]] = (
    # inventory (mod_inventory)
    Pairing("mod_inventory.items", "public.item"),
    Pairing("mod_inventory.warehouses", "public.warehouse"),
    Pairing(
        "mod_inventory.lots",
        "public.inventory_lot",
        renames={"code": "lot_number", "received_at": "received_date", "supplier_lot_ref": "supplier_lot_number"},
        lookups={"status": ("lot_status", "id", "status")},
        note="status derives at enrolment from balance evidence "
        "(is_quarantined flag, then on-hand total: zero = depleted, "
        "else available) via the lot_status map",
    ),
    Pairing("mod_inventory.lot_balances", "public.inventory_lot_balance"),
    Pairing("mod_inventory.serials", "public.inventory_serial"),
    Pairing(
        "mod_inventory.stock_movements",
        "public.inventory_transaction",
        renames={
            "movement_group_id": "transaction_id",
            "kind": "transaction_type",
            "quantity_delta": "quantity",
            "value_delta": "total_cost",
            "occurred_at": "transaction_date",
            "actor_ref": "created_by_user_id",
        },
        note="value_after stays MISSING by design: movements replay "
        "through the inventory costing engine in transaction order and "
        "value_after is its output (parity-checked against the legacy "
        "WAC ledger); copying any legacy column here would fake costing",
    ),
    Pairing("mod_inventory.movement_serials", "public.inventory_serial_movement"),
    Pairing("mod_inventory.stock_reservations", "public.stock_reservation"),
    Pairing(
        "mod_inventory.valuation_snapshots",
        "public.inventory_valuation",
        renames={"as_of_date": "valuation_date", "write_down": "write_down_amount"},
    ),
    *_generated(
        "mod_inventory",
        ("stock_balances",),
        "GENERATED: rebuildable projection from movements (parity-checked, never imported)",
    ),
    # projects (mod_projects)
    Pairing("mod_projects.projects", "public.project"),
    Pairing(
        "mod_projects.project_templates",
        "public.project_template",
        computed={"code": "slug:name"},
        note="code slugified from name (legacy has no template code); "
        "slug collisions surface as enrolment conflicts to rename, "
        "never silently suffixed",
    ),
    Pairing(
        "mod_projects.project_template_tasks",
        "public.project_template_task",
        renames={"title": "task_name", "sort_order": "order_index"},
        constants={"status": "todo", "priority": "medium"},
        computed={"key": "slug:title"},
        note="status/priority are declared enrolment values for template "
        "definitions (TaskStatus.TODO / Priority.MEDIUM) - explicit "
        "constants here, re-ruled per tenant at rehearsal if wanted",
    ),
    Pairing(
        "mod_projects.project_template_task_dependencies",
        "public.project_template_task_dependency",
        lookups={"template_id": ("project_template_task", "template_task_id", "template_id")},
        note="template via the parent-task lookup; tenant from the run extent",
    ),
    Pairing(
        "mod_projects.project_tasks",
        "public.milestone",
        constants={"priority": "medium"},
        note="priority is a declared enrolment value (Priority.MEDIUM) - "
        "legacy milestones carry none; legacy task estate re-ruled "
        "per-file at rehearsal; project finance stays e2",
    ),
    *_generated(
        "mod_projects",
        ("project_task_dependencies", "project_task_assignees"),
        "GENERATED/split from legacy task rows at rehearsal",
    ),
    # ticketing (mod_tkt, tenant plane)
    Pairing("mod_tkt.tickets", "public.ticket"),
    Pairing(
        "mod_tkt.ticket_comments",
        "public.ticket_comment",
        renames={"body": "content"},
        note="tenant from the run extent (legacy comments carry no org column); "
        "action/old/new deltas are audit residue",
    ),
    # satellites with no row import
    *_generated(
        "mod_analytics",
        (
            "metric_catalog_entries",
            "metric_ingest_receipts",
            "metric_observations",
            "metric_points",
            "metric_projection_rebuilds",
        ),
        "GENERATED: metrics re-declared; observations rebuilt from authoritative module rows post-flip",
    ),
    *_generated(
        "mod_aiops",
        ("ai_policies", "ai_policy_versions", "ai_operations", "ai_execution_attempts", "ai_insights"),
        "GENERATED: coach prompts/context are e2 product code over the advisory protocol",
    ),
    Pairing(
        "mod_documents.documents",
        "public.hr_document",
        renames={"state": "status"},
        constants={"type_version": 1},
        lookups={
            "library_id": ("document_library", "tenant_id", "library_id"),
            "type_code": ("document_type", "id", "type_code"),
            "folder_path": ("document_type", "id", "folder_path"),
        },
        note="library/type/folder resolve from enrolment (minted HR "
        "library, category-slug type codes, legacy file_path folders). "
        "sensitivity/handling_instructions stay MISSING: classification "
        "policy rulings for HR documents - never invented per row. "
        "state values verified against the module state set at rehearsal",
    ),
    *_generated(
        "mod_documents",
        (
            "document_libraries",
            "document_type_versions",
            "document_versions",
            "document_renditions",
            "document_classifications",
            "document_relations",
            "document_checkouts",
            "document_annotations",
            "document_access_grants",
            "document_acknowledgements",
            "document_events",
        ),
        "GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage",
    ),
    # forms (mod_forms): near 1:1 plural shapes
    Pairing("mod_forms.forms", "public.form"),
    Pairing(
        "mod_forms.form_versions",
        "public.form_version",
        renames={"settings": "settings_json"},
        fk_rewrites={"status": "form_version_status_by_legacy"},
    ),
    Pairing(
        "mod_forms.form_sections",
        "public.form_section",
        renames={"version_id": "form_version_id", "position": "sort_order"},
        computed={"key": "slug:title"},
        note="key slugified from title; tenant from the run extent",
    ),
    Pairing(
        "mod_forms.form_fields",
        "public.form_field",
        renames={
            "version_id": "form_version_id",
            "key": "field_key",
            "required": "is_required",
            "settings": "settings_json",
            "validation": "validation_json",
            "position": "sort_order",
        },
    ),
    Pairing(
        "mod_forms.form_field_options",
        "public.form_field_option",
        renames={"position": "sort_order", "active": "is_active"},
        note="tenant from the run extent",
    ),
    Pairing(
        "mod_forms.form_submissions",
        "public.form_submission",
        computed={"submission_key": "source_key"},
        note="submission_key is the stable legacy source key; request_fingerprint computes over the imported row",
    ),
    Pairing(
        "mod_forms.form_answers",
        "public.form_answer",
        json_columns=("value_json",),
        note="legacy answers include bare text JSON scalars, re-encoded for the json column",
    ),
    *_generated(
        "mod_inbox",
        ("conversations", "messages", "conversation_read_states", "message_transport_refs"),
        "GENERATED post-flip: legacy email_delivery is kernel-outbox evidence, not inbox state",
    ),
    *_generated(
        "mod_timers",
        ("timers", "timer_acceptances", "timer_rejections"),
        "GENERATED post-flip: timers are live scheduling state, re-armed by owners at the flip",
    ),
    Pairing(
        "mod_workflow.workflow_executions",
        "public.workflow_execution",
        renames={
            "definition_version_ref": "rule_id",
            "subject_ref": "entity_id",
            "source_event_id": "execution_id",
        },
        fk_rewrites={"status": "workflow_status_by_legacy"},
        note="definition_digest/request_fingerprint compute over the "
        "imported row; tenant/source_owner from the run extent; only "
        "executions live at the flip enrol (drain-first ruling)",
    ),
    *_generated(
        "mod_workflow",
        ("workflow_checkpoints", "workflow_repairs"),
        "GENERATED: checkpoints only for executions live at the flip (drain-first ruling)",
    ),
)

ALL_WAVE_PAIRINGS: Final = WAVE3_PAIRINGS + WAVE4_PAIRINGS + WAVE56_PAIRINGS


def build_specs(
    pairings: Sequence[Pairing],
    source_columns: Mapping[str, Sequence[str]],
    destination_tables: Mapping[str, object],
) -> tuple[tuple[TransformSpec, ...], tuple[Pairing, ...]]:
    """Same contract as wave2.build_wave2_specs, for any pairing set."""

    specs: list[TransformSpec] = []
    generated: list[Pairing] = []
    for pairing in pairings:
        if pairing.source is None:
            generated.append(pairing)
            continue
        specs.append(
            build_spec(
                source_table=pairing.source,
                destination_table=pairing.destination,
                source_columns=source_columns[pairing.source],
                destination_columns=destination_columns_from_table(destination_tables[pairing.destination]),
                renames=pairing.renames,
                constants=pairing.constants,
                fk_rewrites=pairing.fk_rewrites,
                lookups=pairing.lookups,
                computed=pairing.computed,
                null_fallbacks=pairing.null_fallbacks,
                deferred=pairing.deferred,
                json_columns=pairing.json_columns,
                sub_origin=pairing.sub_origin,
                sub_customer_ref=pairing.sub_customer_ref,
                source_row_refusal=pairing.source_row_refusal,
            )
        )
    return tuple(specs), tuple(generated)


__all__ = [
    "ALL_WAVE_PAIRINGS",
    "WAVE3_PAIRINGS",
    "WAVE4_PAIRINGS",
    "WAVE56_PAIRINGS",
    "build_specs",
]
