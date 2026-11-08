# Wave-4 transform specs - revenue

Generated from `src/erp_v2/importer/waves36.py` against the real
legacy census and released module models. Same reading rules as mapping-wave2.md.

## public.customer -> mod_customers.customer_accounts
- executable: True; mapped: 6; missing: none
- residue (39): ar_control_account_id, bank_details, billing_address, created_by_user_id, credit_hold, credit_limit, credit_terms_days, currency_code, customer_group_id, customer_type, default_revenue_account_id, default_tax_code_id, default_wht_code_id, dotmac_sub_id, dotmac_sub_metrics, dotmac_sub_reseller_id, erpnext_id, is_active, is_related_party, is_vat_exempt, is_wht_applicable, parent_customer_id, payment_terms_id, price_list_id ...

## public.customer -> mod_customers.customer_profiles
- executable: False; mapped: 4; missing: ['account_id']
- residue (41): ar_control_account_id, bank_details, billing_address, created_by_user_id, credit_hold, credit_limit, credit_terms_days, currency_code, customer_code, customer_group_id, customer_type, default_revenue_account_id, default_tax_code_id, default_wht_code_id, dotmac_sub_id, dotmac_sub_metrics, dotmac_sub_reseller_id, erpnext_id, is_active, is_related_party, is_vat_exempt, is_wht_applicable, legal_name, parent_customer_id ...

## public.contract -> mod_agreements.agreements
- executable: False; mapped: 4; missing: ['reference', 'agreement_family_id', 'counterparty_ref', 'agreement_type', 'effective_date', 'expiry_date']
- residue (20): approval_status, collectability_assessment, consideration_payable, contract_name, contract_number, contract_type, currency_code, customer_id, end_date, financing_rate, has_commercial_substance, is_enforceable, modification_history, noncash_consideration, organization_id, significant_financing, start_date, terms_and_conditions, total_contract_value, variable_consideration

## public.contract_amendment_workflow -> mod_agreements.agreement_events
- executable: False; mapped: 2; missing: ['agreement_id', 'sequence', 'event_type', 'to_status', 'command_id']
- residue (16): amendment_workflow_id, appraisee_id, appraisee_signed_date, appraiser_id, appraiser_signed_date, contract_id, hod_id, hod_signed_date, hr_head_id, hr_head_signed_date, organization_id, original_contract_id, rejected_by_stage, rejection_reason, signoff_note, status

## public.quote -> mod_sales.quotes
- executable: False; mapped: 15; missing: ['lead_id', 'tax_rate', 'authored_by_kind', 'authored_by_opaque_id', 'authored_by_label']
- residue (18): contact_email, contact_name, converted_at, converted_to_invoice_id, converted_to_so_id, created_by, customer_id, exchange_rate, internal_notes, payment_terms_id, quote_date, quote_number, reference, rejection_reason, sent_by, terms_and_conditions, updated_by, viewed_at

## public.quote_line -> mod_sales.quote_lines
- executable: False; mapped: 6; missing: ['position', 'gross_amount', 'amount']
- residue (10): cost_center_id, discount_percent, item_code, line_id, line_number, line_total, project_id, revenue_account_id, tax_code_id, unit_of_measure

## GENERATED / command-import destinations
- mod_billing.settlement_earmarks: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.settlement_earmark_targets: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.settlement_earmark_effects: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.opening_windows: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.opening_window_admissions: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.opening_window_seals: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.receivable_source_claims: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.opening_receivable_bases: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.opening_historical_discharge_claims: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.receivable_exposure_facts_v2: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.opening_import_receipts: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.opening_cash_lots: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.opening_cash_effects: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.cash_source_claims: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.non_cash_credit_grants: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.non_cash_credit_effects: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.receivable_position_facts_v2: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.withholding_discharges: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.billing_accounts: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.rated_obligations: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.documents: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.document_lines: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.document_events: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.confirmed_settlements: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.posting_groups: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.posting_effects: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.allocation_effects: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.applied_tax_snapshots: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.applied_fx_snapshots: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.party_tax_identity_snapshots: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.invoice_document_facts: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.document_artifacts: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.accounting_facts: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.receivable_position_facts: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_billing.receivable_exposure_facts: COMMAND-IMPORT: legacy invoice/invoice_line/customer_payment enter via opening-window commands (opening receivables/cash/discharges), never 
- mod_customers.customer_party_references: GENERATED: party link resolved against kernel Party import
- mod_agreements.agreement_lines: GENERATED/split at rehearsal
- mod_agreements.agreement_approval_withdrawals: GENERATED/split at rehearsal
- mod_sales.pipelines: GENERATED: pipeline is enrolment config; no legacy lead estate in ERP
- mod_sales.pipeline_stages: GENERATED: pipeline is enrolment config; no legacy lead estate in ERP
- mod_sales.leads: GENERATED: pipeline is enrolment config; no legacy lead estate in ERP
- mod_sales.lead_origins: GENERATED: pipeline is enrolment config; no legacy lead estate in ERP
- mod_sales.quote_discount_revisions: GENERATED: pipeline is enrolment config; no legacy lead estate in ERP
- mod_coll.collection_policies: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_policy_versions: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_policy_steps: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_cases: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_case_exposures: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_case_transitions: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_step_attempts: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.payment_arrangements: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.payment_arrangement_exposures: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.payment_arrangement_installments: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.payment_arrangement_settlement_receipts: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_grace_grants: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_notice_requests: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_notice_receipts: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_action_requests: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_action_receipts: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_coll.collection_reconciliations: GENERATED: cases open post-flip from billing exposures; policy is enrolment config
- mod_party.party_roles: GENERATED at identity enrolment from kernel Party + employee/customer/supplier links
- mod_party.party_relationships: GENERATED at identity enrolment from kernel Party + employee/customer/supplier links
- mod_party.party_memberships: GENERATED at identity enrolment from kernel Party + employee/customer/supplier links
- mod_party.party_contact_points: GENERATED at identity enrolment from kernel Party + employee/customer/supplier links
- mod_party.party_external_references: GENERATED at identity enrolment from kernel Party + employee/customer/supplier links

## Unpaired module destinations
none
