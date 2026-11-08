# Wave-3 transform specs - finance core

Generated from `src/erp_v2/importer/waves36.py` against the real
legacy census and released module models. Same reading rules as mapping-wave2.md.

## public.account -> mod_accounting.accounts
- executable: True; mapped: 11; missing: none
- residue (13): created_by_user_id, default_currency_code, deferral_pair_account_id, is_budgetable, is_cash_equivalent, is_deferral, is_financial_instrument, is_multi_currency, is_posting_allowed, is_reconciliation_required, search_terms, subledger_type, updated_by_user_id

## public.account_category -> mod_accounting.account_categories
- executable: True; mapped: 8; missing: none
- residue (4): display_order, financial_statement_line_id, hierarchy_level, parent_category_id

## public.fiscal_year -> mod_accounting.fiscal_years
- executable: True; mapped: 7; missing: none
- residue (5): closed_at, closed_by_user_id, is_adjustment_year, is_closed, retained_earnings_account_id

## public.fiscal_period -> mod_accounting.fiscal_periods
- executable: True; mapped: 9; missing: none
- residue (8): hard_closed_at, hard_closed_by_user_id, is_adjustment_period, is_closing_period, last_reopen_session_id, reopen_count, soft_closed_at, soft_closed_by_user_id

## public.coa_segment_definition -> mod_accounting.accounting_dimensions
- executable: True; mapped: 5; missing: none
- residue (5): code_length, code_position_start, display_order, is_required, separator

## public.coa_segment_value -> mod_accounting.accounting_dimension_values
- executable: True; mapped: 7; missing: none
- residue (1): parent_segment_value_id

## public.journal_entry -> mod_accounting.journal_entries
- executable: True; mapped: 23; missing: none
- residue (15): approval_request_id, approved_at, approved_by_user_id, auto_reverse_date, correlation_id, created_by_user_id, exchange_rate_type_id, intercompany_org_id, is_intercompany, is_reversal, matching_journal_id, posting_batch_id, source_module, submitted_at, submitted_by_user_id

## public.journal_entry_line -> mod_accounting.journal_lines
- executable: True; mapped: 10; missing: none
- residue (7): business_unit_id, cost_center_id, currency_code, exchange_rate, project_id, reconciliation_id, segment_id

## public.posted_ledger_line -> mod_accounting.posted_ledger_lines
- executable: True; mapped: 21; missing: none
- residue (8): business_unit_id, correlation_id, cost_center_id, posting_batch_id, posting_year, project_id, segment_id, source_module

## public.bank_directory -> mod_banking.bank_institutions
- executable: True; mapped: 3; missing: none
- residue (4): aliases, is_active, nibss_code, sort_code_prefix

## public.bank_accounts -> mod_banking.bank_accounts
- executable: True; mapped: 12; missing: none
- residue (27): allow_overdraft, bank_name, branch_code, branch_name, contact_email, contact_name, contact_phone, created_by, iban, is_primary, last_reconciled_balance, last_reconciled_date, last_statement_balance, last_statement_date, mono_account_id, mono_last_ingest_at, mono_last_sync_error, mono_last_synced_at, mono_last_transaction_date, mono_last_transaction_sync_at, mono_link_failed, mono_sync_buffer_days, mono_sync_from_date, mono_transaction_sync_status ...

## public.bank_statements -> mod_banking.bank_statements
- executable: True; mapped: 16; missing: none
- residue (8): import_filename, import_source, imported_by, notes, statement_date, statement_number, unmatched_lines, updated_at

## public.bank_statement_lines -> mod_banking.bank_statement_lines
- executable: True; mapped: 13; missing: none
- residue (14): bank_category, bank_code, categorization_status, check_number, matched_by, notes, payee_payer, raw_data, running_balance, suggested_account_id, suggested_confidence, suggested_match_reason, suggested_rule_id, transaction_id

## public.supplier_invoice -> mod_payables.supplier_invoices
- executable: True; mapped: 18; missing: none
- residue (30): amount_paid, approval_request_id, approved_by_user_id, auto_create_inventory_receipt, balance_due, comments, correlation_id, created_by_user_id, due_date, exchange_rate_type_id, functional_currency_amount, intercompany_org_id, inventory_receipt_mode, invoice_type, is_intercompany, is_prepayment, journal_entry_id, posted_at, posted_by_user_id, posting_batch_id, posting_status, prepayment_applied, purpose, stamp_duty_amount ...

## public.supplier_invoice_line -> mod_payables.supplier_invoice_lines
- executable: True; mapped: 10; missing: none
- residue (16): asset_account_id, asset_category_id, auto_receipt_transaction_id, capitalize_flag, cost_center_id, created_asset_id, goods_receipt_line_id, item_id, po_line_id, project_id, receipt_auto_generate_serials, receipt_reference, receipt_serial_numbers, receipt_warehouse_id, segment_id, tax_code_id

## public.supplier_payment -> mod_payables.settlement_observations
- executable: False; mapped: 6; missing: ['obligation_id']
- residue (25): approval_request_id, approved_at, approved_by_user_id, bank_account_id, bank_reconciliation_id, correlation_id, created_by_user_id, exchange_rate, functional_currency_amount, gross_amount, journal_entry_id, payment_batch_id, payment_method, payment_number, posted_at, posted_by_user_id, posting_batch_id, reference, remittance_advice_sent, remittance_sent_at, status, supplier_id, updated_at, withholding_tax_amount ...

## public.payment_intent -> mod_payments.payment_intents
- executable: False; mapped: 11; missing: ['payer_reference', 'purpose', 'channel']
- residue (21): authorization_url, bank_account_id, customer_payment_id, direction, email, fee_amount, fee_journal_id, gateway_response, intent_metadata, last_poll_error, paystack_access_code, paystack_transaction_id, poll_count, recipient_account_name, recipient_account_number, recipient_bank_code, source_id, source_type, transfer_code, transfer_recipient_code, unresolved_since

## public.customer_payment -> mod_payments.payment_confirmations
- executable: True; mapped: 9; missing: none
- residue (24): bank_account_id, bank_reconciliation_id, correlation_id, created_by_user_id, customer_id, description, dotmac_sub_id, dotmac_sub_receipt_number, erpnext_id, exchange_rate, functional_currency_amount, gross_amount, journal_entry_id, last_synced_at, payment_number, posted_at, posted_by_user_id, posting_batch_id, splynx_id, splynx_receipt_number, status, wht_amount, wht_certificate_number, wht_code_id

## public.expense_category -> mod_expenses.expense_categories
- executable: True; mapped: 10; missing: none
- residue (1): expense_account_id

## public.expense_claim -> mod_expenses.expense_claims
- executable: True; mapped: 15; missing: none
- residue (19): advance_adjusted, approved_on, approver_id, cash_advance_id, cost_center_id, net_payable_amount, paid_on, payment_reference, project_id, recipient_account_number, recipient_bank_code, recipient_bank_name, recipient_name, rejection_reason, requested_approver_id, supplier_invoice_id, task_id, ticket_id, vehicle_id

## public.expense_claim_item -> mod_expenses.expense_claim_lines
- executable: True; mapped: 12; missing: none
- residue (8): cost_center_id, distance_km, expense_account_id, is_travel_expense, receipt_number, receipt_url, travel_from, travel_to

## public.expense_limit_rule -> mod_expenses.expense_policies
- executable: True; mapped: 10; missing: none
- residue (13): action_config, action_type, block_count, custom_period_days, dimension_filters, evaluation_count, is_active, limit_amount, period_type, priority, scope_id, scope_type, trigger_count

## public.expense_limit_evaluation -> mod_expenses.expense_policy_evaluations
- executable: True; mapped: 10; missing: none
- residue (5): context_data, evaluated_by_id, period_spent_amount, result_message, rule_code

## public.expense_claim_action -> mod_expenses.expense_lifecycle_events
- executable: False; mapped: 6; missing: ['actor_reference']
- residue (2): action_key, action_type

## public.expense_entry -> mod_expenses.expense_requests
- executable: False; mapped: 12; missing: ['needed_by']
- residue (20): approved_at, approved_by, business_unit_id, cost_center_id, expense_account_id, expense_date, journal_entry_id, payee, payment_account_id, payment_method, posted_at, posted_by, project_id, receipt_reference, submitted_by, task_id, tax_amount, tax_code_id, ticket_id, updated_by

## public.purchase_requisition -> mod_procurement.purchase_requisitions
- executable: True; mapped: 10; missing: none
- residue (9): approval_request_id, approved_by_user_id, budget_verified, budget_verified_at, budget_verified_by_id, created_by_user_id, department_id, material_request_id, plan_item_id

## public.purchase_requisition_line -> mod_procurement.purchase_requisition_lines
- executable: True; mapped: 10; missing: none
- residue (3): delivery_date, expense_account_id, project_id

## public.request_for_quotation -> mod_procurement.sourcing_events
- executable: True; mapped: 11; missing: none
- residue (3): plan_item_id, requisition_id, terms_and_conditions

## public.rfq_invitation -> mod_procurement.sourcing_invitations
- executable: True; mapped: 4; missing: none
- residue (2): responded, response_date

## public.quotation_response -> mod_procurement.bid_submissions
- executable: True; mapped: 12; missing: none
- residue (2): notes, response_date

## public.quotation_response_line -> mod_procurement.bid_lines
- executable: True; mapped: 8; missing: none
- residue (1): delivery_date

## public.bid_evaluation -> mod_procurement.bid_evaluations
- executable: False; mapped: 7; missing: ['scores_json', 'selected_total_score']
- residue (4): approval_request_id, approved_by_user_id, evaluation_report, recommended_supplier_id

## public.purchase_order -> mod_procurement.purchase_orders
- executable: True; mapped: 15; missing: none
- residue (13): amendment_reason, amendment_version, approval_request_id, approved_by_user_id, budget_id, commitment_journal_entry_id, correlation_id, created_by_user_id, exchange_rate, is_amendment, original_po_id, shipping_address, variation_id

## public.purchase_order_line -> mod_procurement.purchase_order_lines
- executable: True; mapped: 12; missing: none
- residue (7): asset_account_id, delivery_date, expense_account_id, project_id, quantity_invoiced, segment_id, tax_code_id

## public.goods_receipt -> mod_procurement.receipt_observations
- executable: True; mapped: 6; missing: none
- residue (6): notes, receipt_number, received_by_user_id, status, supplier_id, warehouse_id

## public.approval_request -> mod_approvals.approval_requests
- executable: True; mapped: 8; missing: none
- residue (8): correlation_id, document_amount, document_currency_code, document_reference, final_approver_user_id, notes, requested_at, workflow_id

## public.approval_decision -> mod_approvals.approval_decisions
- executable: True; mapped: 7; missing: none
- residue (3): comments, delegated_from_user_id, ip_address

## public.approval_workflow -> mod_approvals.approval_policies
- executable: True; mapped: 6; missing: none
- residue (6): description, document_type, is_active, threshold_amount, threshold_currency_code, workflow_name

## public.asset -> mod_assets.assets
- executable: False; mapped: 14; missing: ['condition']
- residue (32): accumulated_depreciation, acquisition_cost, cash_generating_unit_id, category_id, cost_center_id, created_by_user_id, currency_code, current_depreciation_schedule_id, custodian_employee_id, depreciation_method, depreciation_start_date, disposal_date, disposal_gain_loss, disposal_proceeds, functional_currency_cost, impairment_loss, in_service_date, insurance_policy_number, insured_value, invoice_reference, is_component_parent, net_book_value, parent_asset_id, project_id ...

## public.asset_assignment -> mod_assets.asset_assignments
- executable: True; mapped: 11; missing: none
- residue (3): expected_return_date, returned_on, transfer_from_assignment_id

## public.asset_disposal -> mod_assets.asset_disposals
- executable: True; mapped: 13; missing: none
- residue (13): accumulated_depreciation_at_disposal, buyer_name, cost_at_disposal, costs_of_disposal, disposal_proceeds, fiscal_period_id, gain_loss_on_disposal, insurance_claim_reference, insurance_proceeds, invoice_number, net_book_value_at_disposal, net_proceeds, trade_in_asset_id

## public.asset_lifecycle_event -> mod_assets.asset_lifecycle_events
- executable: True; mapped: 9; missing: none
- residue (10): actor_user_id, event_category, event_payload, new_owner_employee_id, new_status, previous_owner_employee_id, previous_status, source_record_id, source_type, updated_at

## public.depreciation_run -> mod_finance.depreciation_runs
- executable: False; mapped: 9; missing: ['currency_code']
- residue (7): calculation_started_at, created_at, created_by_user_id, journal_entry_id, posted_by_user_id, posting_batch_id, run_description

## GENERATED / command-import destinations
- mod_accounting.journal_line_dimensions: GENERATED: dimension rows split from legacy line columns; period events from fiscal period state
- mod_accounting.posted_ledger_dimensions: GENERATED: dimension rows split from legacy line columns; period events from fiscal period state
- mod_accounting.period_events: GENERATED: dimension rows split from legacy line columns; period events from fiscal period state
- mod_banking.match_allocations: GENERATED: allocations are outputs of match decisions over cash observations (both GENERATED); legacy bank_statement_line_matches rows are r
- mod_banking.reconciliations: GENERATED: a module reconciliation binds one statement snapshot; legacy bank_reconciliations are per-account period reports with no statemen
- mod_banking.cash_account_observations: GENERATED: observations arrive via Integrator/adapters; policies are tenant config at enrolment
- mod_banking.match_policies: GENERATED: observations arrive via Integrator/adapters; policies are tenant config at enrolment
- mod_banking.match_decisions: GENERATED: observations arrive via Integrator/adapters; policies are tenant config at enrolment
- mod_payables.credit_notes: GENERATED/derived by payables service from imported invoices; legacy credit notes (invoice type) split at rehearsal
- mod_payables.credit_note_lines: GENERATED/derived by payables service from imported invoices; legacy credit notes (invoice type) split at rehearsal
- mod_payables.payment_obligations: GENERATED/derived by payables service from imported invoices; legacy credit notes (invoice type) split at rehearsal
- mod_payables.liability_events: GENERATED/derived by payables service from imported invoices; legacy credit notes (invoice type) split at rehearsal
- mod_payables.credit_applications: GENERATED/derived by payables service from imported invoices; legacy credit notes (invoice type) split at rehearsal
- mod_payables.accounting_receipts: GENERATED/derived by payables service from imported invoices; legacy credit notes (invoice type) split at rehearsal
- mod_payments.payment_transfer_proofs: GENERATED: merchant context is enrolment config; proofs/qualifications are provider facts post-flip
- mod_payments.merchant_contexts: GENERATED: merchant context is enrolment config; proofs/qualifications are provider facts post-flip
- mod_payments.merchant_context_ends: GENERATED: merchant context is enrolment config; proofs/qualifications are provider facts post-flip
- mod_payments.provider_cash_qualifications: GENERATED: merchant context is enrolment config; proofs/qualifications are provider facts post-flip
- mod_expenses.expense_request_lines: GENERATED/split at rehearsal: lines and rules derive from legacy rows; receipts reference files-module ids
- mod_expenses.expense_policy_rules: GENERATED/split at rehearsal: lines and rules derive from legacy rows; receipts reference files-module ids
- mod_expenses.expense_receipts: GENERATED/split at rehearsal: lines and rules derive from legacy rows; receipts reference files-module ids
- mod_procurement.sourcing_event_lines: GENERATED: sourcing lines snapshot from requisition lines; evidence is digest-bound input
- mod_procurement.procurement_evidence: GENERATED: sourcing lines snapshot from requisition lines; evidence is digest-bound input
- mod_approvals.approval_withdrawals: GENERATED post-flip (no legacy withdrawal state)
- mod_assets.asset_maintenance: GENERATED post-flip: legacy asset_tracking_event rows are GPS/scan tracking observations (latitude/longitude/tracked_at), not maintenance re
- mod_finance.depreciation_lines: GENERATED: module lines are computed from enrolled asset books incl. the unimpaired-carrying shadow legacy never stored; legacy depreciation
- mod_finance.asset_books: GENERATED: books enrol per asset; consequences are balanced outputs of impairment/revaluation/depreciation events
- mod_finance.accounting_events: GENERATED: books enrol per asset; consequences are balanced outputs of impairment/revaluation/depreciation events
- mod_finance.accounting_consequences: GENERATED: books enrol per asset; consequences are balanced outputs of impairment/revaluation/depreciation events
- mod_finance.accounting_consequence_lines: GENERATED: books enrol per asset; consequences are balanced outputs of impairment/revaluation/depreciation events

## Unpaired module destinations
none
