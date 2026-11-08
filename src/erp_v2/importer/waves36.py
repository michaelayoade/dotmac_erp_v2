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

from erp_v2.importer.spec import TransformSpec, build_spec, destination_columns_from_table
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
    Pairing("mod_accounting.accounts", "public.account"),
    Pairing(
        "mod_accounting.account_categories",
        "public.account_category",
        renames={"account_class": "ifrs_category"},
    ),
    Pairing("mod_accounting.fiscal_years", "public.fiscal_year"),
    Pairing("mod_accounting.fiscal_periods", "public.fiscal_period"),
    Pairing(
        "mod_accounting.accounting_dimensions",
        "public.coa_segment_definition",
        renames={"code": "segment_type", "name": "segment_name"},
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
        note="source_owner/source_version run-supplied; approval_reference "
        "from legacy approval_request_id at rehearsal",
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
            "journal_number": "journal_reference",
            "currency_code": "original_currency_code",
            "debit": "debit_amount",
            "credit": "credit_amount",
            "original_debit": "original_debit_amount",
            "original_credit": "original_credit_amount",
            "source_document_kind": "source_document_type",
            "posted_by": "posted_by_user_id",
        },
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
        note="legacy directory is the Nigerian NIBSS bank list (nibss_code); country fixed NG",
    ),
    Pairing(
        "mod_banking.bank_accounts",
        "public.bank_accounts",
        renames={
            "institution_id": "bank_code",
            "account_code": "account_number",
            "account_identifier": "account_number",
            "account_type_code": "account_type",
            "cash_account_ref": "gl_account_id",
        },
        fk_rewrites={"institution_id": "bank_institution_by_code"},
        note="institutions enrol from bank_directory/bank codes first; mono_* sync state is adapter residue",
    ),
    Pairing(
        "mod_banking.bank_statements",
        "public.bank_statements",
        renames={"account_id": "bank_account_id"},
    ),
    Pairing("mod_banking.bank_statement_lines", "public.bank_statement_lines"),
    Pairing("mod_banking.match_allocations", "public.bank_statement_line_matches"),
    Pairing("mod_banking.reconciliations", "public.bank_reconciliations"),
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
        },
        note="supplier_name_snapshot joins supplier master at rehearsal; payment_schedule derives from due_date/terms",
    ),
    Pairing(
        "mod_payables.supplier_invoice_lines",
        "public.supplier_invoice_line",
        renames={"posting_account_ref": "expense_account_id"},
        note="dimension_refs assembled from cost_center/project/segment at rehearsal",
    ),
    Pairing("mod_payables.settlement_observations", "public.supplier_payment"),
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
        note="legacy intent table is Paystack-specific; payer/purpose/channel "
        "derive from source_type/source_id at rehearsal",
    ),
    Pairing(
        "mod_payments.payment_confirmations",
        "public.customer_payment",
        renames={
            "confirmed_amount": "amount",
            "observed_at": "payment_date",
            "external_reference": "reference",
            "provider_type": "payment_method",
        },
        note="intent linkage via payment_intent.customer_payment_id at rehearsal; "
        "webhook evidence attaches as provenance",
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
        note="batch_id groups evaluations per import run at rehearsal",
    ),
    Pairing("mod_expenses.expense_lifecycle_events", "public.expense_claim_action"),
    Pairing("mod_expenses.expense_requests", "public.expense_entry"),
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
    Pairing("mod_procurement.sourcing_events", "public.request_for_quotation"),
    Pairing("mod_procurement.sourcing_invitations", "public.rfq_invitation"),
    Pairing("mod_procurement.bid_submissions", "public.quotation_response"),
    Pairing("mod_procurement.bid_lines", "public.quotation_response_line"),
    Pairing("mod_procurement.bid_evaluations", "public.bid_evaluation"),
    Pairing(
        "mod_procurement.purchase_orders",
        "public.purchase_order",
        renames={
            "order_number": "po_number",
            "ordered_on": "po_date",
            "terms": "terms_and_conditions",
        },
        note="source requisition/evaluation links resolve via procurement evidence at rehearsal",
    ),
    Pairing("mod_procurement.purchase_order_lines", "public.purchase_order_line"),
    Pairing("mod_procurement.receipt_observations", "public.goods_receipt"),
    *_generated(
        "mod_procurement",
        ("sourcing_event_lines", "procurement_evidence"),
        "GENERATED: sourcing lines snapshot from requisition lines; evidence is digest-bound input",
    ),
    # approvals (mod_approvals, tenant plane)
    Pairing("mod_approvals.approval_requests", "public.approval_request"),
    Pairing("mod_approvals.approval_decisions", "public.approval_decision"),
    Pairing(
        "mod_approvals.approval_policies",
        "public.approval_workflow",
        note="policy is re-published as a version through the approvals service at enrolment",
    ),
    *_generated(
        "mod_approvals",
        ("approval_withdrawals",),
        "GENERATED post-flip (no legacy withdrawal state)",
    ),
    # finance / fixed assets (mod_finance) + assets (mod_assets)
    Pairing("mod_assets.assets", "public.asset"),
    Pairing("mod_assets.asset_assignments", "public.asset_assignment"),
    Pairing("mod_assets.asset_disposals", "public.asset_disposal"),
    Pairing("mod_assets.asset_lifecycle_events", "public.asset_lifecycle_event"),
    Pairing(
        "mod_assets.asset_maintenance",
        "public.asset_tracking_event",
        note="maintenance-shaped tracking events only; remainder is lifecycle evidence",
    ),
    Pairing("mod_finance.depreciation_runs", "public.depreciation_run"),
    Pairing("mod_finance.depreciation_lines", "public.depreciation_schedule"),
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
    Pairing("mod_customers.customer_accounts", "public.customer"),
    Pairing(
        "mod_customers.customer_profiles",
        "public.customer",
        note="second spec from the same legacy row (profile columns split)",
    ),
    *_generated(
        "mod_customers",
        ("customer_party_references",),
        "GENERATED: party link resolved against kernel Party import",
    ),
    Pairing(
        "mod_agreements.agreements",
        "public.contract",
        note="commercial contracts only; employment/lease/performance contracts are e2",
    ),
    Pairing("mod_agreements.agreement_events", "public.contract_amendment_workflow"),
    *_generated(
        "mod_agreements",
        ("agreement_lines", "agreement_approval_withdrawals"),
        "GENERATED/split at rehearsal",
    ),
    Pairing("mod_sales.quotes", "public.quote"),
    Pairing("mod_sales.quote_lines", "public.quote_line"),
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
    Pairing("mod_inventory.lots", "public.inventory_lot"),
    Pairing("mod_inventory.lot_balances", "public.inventory_lot_balance"),
    Pairing("mod_inventory.serials", "public.inventory_serial"),
    Pairing("mod_inventory.stock_movements", "public.inventory_transaction"),
    Pairing("mod_inventory.movement_serials", "public.inventory_serial_movement"),
    Pairing("mod_inventory.stock_reservations", "public.stock_reservation"),
    Pairing("mod_inventory.valuation_snapshots", "public.inventory_valuation"),
    *_generated(
        "mod_inventory",
        ("stock_balances",),
        "GENERATED: rebuildable projection from movements (parity-checked, never imported)",
    ),
    # projects (mod_projects)
    Pairing("mod_projects.projects", "public.project"),
    Pairing("mod_projects.project_templates", "public.project_template"),
    Pairing("mod_projects.project_template_tasks", "public.project_template_task"),
    Pairing(
        "mod_projects.project_template_task_dependencies",
        "public.project_template_task_dependency",
    ),
    Pairing(
        "mod_projects.project_tasks",
        "public.milestone",
        note="legacy task estate (tasks/time entries) re-ruled per-file at rehearsal; project finance stays e2",
    ),
    *_generated(
        "mod_projects",
        ("project_task_dependencies", "project_task_assignees"),
        "GENERATED/split from legacy task rows at rehearsal",
    ),
    # ticketing (mod_tkt, tenant plane)
    Pairing("mod_tkt.tickets", "public.ticket"),
    Pairing("mod_tkt.ticket_comments", "public.ticket_comment"),
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
        note="also generated_document/vehicle_document at rehearsal; templates become document types",
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
    Pairing("mod_forms.form_versions", "public.form_version"),
    Pairing("mod_forms.form_sections", "public.form_section"),
    Pairing("mod_forms.form_fields", "public.form_field"),
    Pairing("mod_forms.form_field_options", "public.form_field_option"),
    Pairing("mod_forms.form_submissions", "public.form_submission"),
    Pairing("mod_forms.form_answers", "public.form_answer"),
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
    Pairing("mod_workflow.workflow_executions", "public.workflow_execution"),
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
