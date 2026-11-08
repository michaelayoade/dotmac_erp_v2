# Wave-3 transform specs - finance core

Generated from `src/erp_v2/importer/waves36.py` against the real
legacy census and released module models. Same reading rules as mapping-wave2.md.

## public.account -> mod_accounting.accounts
- executable: True; mapped: 11; missing: none
- residue (13): created_by_user_id, default_currency_code, deferral_pair_account_id, is_budgetable, is_cash_equivalent, is_deferral, is_financial_instrument, is_multi_currency, is_posting_allowed, is_reconciliation_required, search_terms, subledger_type, updated_by_user_id

## public.account_category -> mod_accounting.account_categories
- executable: True; mapped: 7; missing: none
- residue (5): category_id, display_order, financial_statement_line_id, hierarchy_level, parent_category_id

## public.fiscal_year -> mod_accounting.fiscal_years
- executable: True; mapped: 7; missing: none
- residue (5): closed_at, closed_by_user_id, is_adjustment_year, is_closed, retained_earnings_account_id

## public.fiscal_period -> mod_accounting.fiscal_periods
- executable: True; mapped: 9; missing: none
- residue (8): hard_closed_at, hard_closed_by_user_id, is_adjustment_period, is_closing_period, last_reopen_session_id, reopen_count, soft_closed_at, soft_closed_by_user_id

## public.coa_segment_definition -> mod_accounting.accounting_dimensions
- executable: True; mapped: 4; missing: none
- residue (6): code_length, code_position_start, display_order, is_required, segment_def_id, separator

## public.coa_segment_value -> mod_accounting.accounting_dimension_values
- executable: True; mapped: 6; missing: none
- residue (2): parent_segment_value_id, segment_value_id

## public.journal_entry -> mod_accounting.journal_entries
- executable: True; mapped: 23; missing: none
- residue (15): approval_request_id, approved_at, approved_by_user_id, auto_reverse_date, correlation_id, created_by_user_id, exchange_rate_type_id, intercompany_org_id, is_intercompany, is_reversal, matching_journal_id, posting_batch_id, source_module, submitted_at, submitted_by_user_id

## public.journal_entry_line -> mod_accounting.journal_lines
- executable: True; mapped: 9; missing: none
- residue (8): business_unit_id, cost_center_id, currency_code, exchange_rate, line_id, project_id, reconciliation_id, segment_id

## public.posted_ledger_line -> mod_accounting.posted_ledger_lines
- executable: True; mapped: 20; missing: none
- residue (9): business_unit_id, correlation_id, cost_center_id, ledger_line_id, posting_batch_id, posting_year, project_id, segment_id, source_module

## public.bank_directory -> mod_banking.bank_institutions
- executable: True; mapped: 3; missing: none
- residue (4): aliases, is_active, nibss_code, sort_code_prefix

## public.bank_accounts -> mod_banking.bank_accounts
- executable: True; mapped: 12; missing: none
- residue (27): allow_overdraft, bank_name, branch_code, branch_name, contact_email, contact_name, contact_phone, created_by, iban, is_primary, last_reconciled_balance, last_reconciled_date, last_statement_balance, last_statement_date, mono_account_id, mono_last_ingest_at, mono_last_sync_error, mono_last_synced_at, mono_last_transaction_date, mono_last_transaction_sync_at, mono_link_failed, mono_sync_buffer_days, mono_sync_from_date, mono_transaction_sync_status ...

## public.bank_statements -> mod_banking.bank_statements
- executable: True; mapped: 15; missing: none
- residue (8): import_filename, import_source, imported_by, notes, statement_date, statement_number, unmatched_lines, updated_at

## public.bank_statement_lines -> mod_banking.bank_statement_lines
- executable: False; mapped: 10; missing: ['direction', 'external_ref']
- residue (17): bank_category, bank_code, bank_reference, categorization_status, check_number, line_id, matched_by, notes, payee_payer, raw_data, running_balance, suggested_account_id, suggested_confidence, suggested_match_reason, suggested_rule_id, transaction_id, transaction_type

## public.bank_statement_line_matches -> mod_banking.match_allocations
- executable: False; mapped: 0; missing: ['decision_id', 'observation_id', 'amount']
- residue (16): confirmed_at, confirmed_by, idempotency_key, is_primary, journal_line_id, match_group_id, match_id, match_reason, match_score, match_state, match_type, matched_at, matched_by, source_id, source_type, statement_line_id

## public.bank_reconciliations -> mod_banking.reconciliations
- executable: False; mapped: 5; missing: ['statement_id', 'cash_opening_balance', 'cash_closing_balance', 'difference', 'total_lines', 'matched_lines', 'snapshot_ref']
- residue (26): approved_by, bank_account_id, created_at, currency_code, gl_closing_balance, gl_opening_balance, notes, outstanding_deposits, outstanding_payments, period_end, period_start, prepared_by, prior_outstanding_deposits, prior_outstanding_payments, reconciliation_date, reconciliation_difference, reconciliation_id, review_notes, reviewed_at, reviewed_by, statement_opening_balance, total_adjustments, total_matched, total_unmatched_gl ...

## public.supplier_invoice -> mod_payables.supplier_invoices
- executable: False; mapped: 17; missing: ['supplier_name_snapshot', 'payment_schedule']
- residue (31): amount_paid, approval_request_id, approved_by_user_id, auto_create_inventory_receipt, balance_due, comments, correlation_id, created_by_user_id, due_date, exchange_rate_type_id, functional_currency_amount, intercompany_org_id, inventory_receipt_mode, invoice_id, invoice_type, is_intercompany, is_prepayment, journal_entry_id, posted_at, posted_by_user_id, posting_batch_id, posting_status, prepayment_applied, purpose ...

## public.supplier_invoice_line -> mod_payables.supplier_invoice_lines
- executable: False; mapped: 9; missing: ['dimension_refs']
- residue (17): asset_account_id, asset_category_id, auto_receipt_transaction_id, capitalize_flag, cost_center_id, created_asset_id, goods_receipt_line_id, item_id, line_id, po_line_id, project_id, receipt_auto_generate_serials, receipt_reference, receipt_serial_numbers, receipt_warehouse_id, segment_id, tax_code_id

## public.supplier_payment -> mod_payables.settlement_observations
- executable: False; mapped: 3; missing: ['obligation_id', 'occurred_at', 'recorded_at']
- residue (28): approval_request_id, approved_at, approved_by_user_id, bank_account_id, bank_reconciliation_id, correlation_id, created_at, created_by_user_id, exchange_rate, functional_currency_amount, gross_amount, journal_entry_id, payment_batch_id, payment_date, payment_id, payment_method, payment_number, posted_at, posted_by_user_id, posting_batch_id, reference, remittance_advice_sent, remittance_sent_at, status ...

## public.payment_intent -> mod_payments.payment_intents
- executable: False; mapped: 10; missing: ['payer_reference', 'purpose', 'channel']
- residue (22): authorization_url, bank_account_id, customer_payment_id, direction, email, fee_amount, fee_journal_id, gateway_response, intent_id, intent_metadata, last_poll_error, paystack_access_code, paystack_transaction_id, poll_count, recipient_account_name, recipient_account_number, recipient_bank_code, source_id, source_type, transfer_code, transfer_recipient_code, unresolved_since

## public.customer_payment -> mod_payments.payment_confirmations
- executable: False; mapped: 8; missing: ['intent_id', 'source']
- residue (25): bank_account_id, bank_reconciliation_id, correlation_id, created_by_user_id, customer_id, description, dotmac_sub_id, dotmac_sub_receipt_number, erpnext_id, exchange_rate, functional_currency_amount, gross_amount, journal_entry_id, last_synced_at, payment_id, payment_number, posted_at, posted_by_user_id, posting_batch_id, splynx_id, splynx_receipt_number, status, wht_amount, wht_certificate_number ...

## public.expense_category -> mod_expenses.expense_categories
- executable: True; mapped: 9; missing: none
- residue (2): category_id, expense_account_id

## public.expense_claim -> mod_expenses.expense_claims
- executable: True; mapped: 14; missing: none
- residue (20): advance_adjusted, approved_on, approver_id, cash_advance_id, claim_id, cost_center_id, net_payable_amount, paid_on, payment_reference, project_id, recipient_account_number, recipient_bank_code, recipient_bank_name, recipient_name, rejection_reason, requested_approver_id, supplier_invoice_id, task_id, ticket_id, vehicle_id

## public.expense_claim_item -> mod_expenses.expense_claim_lines
- executable: True; mapped: 11; missing: none
- residue (9): cost_center_id, distance_km, expense_account_id, is_travel_expense, item_id, receipt_number, receipt_url, travel_from, travel_to

## public.expense_limit_rule -> mod_expenses.expense_policies
- executable: True; mapped: 9; missing: none
- residue (14): action_config, action_type, block_count, custom_period_days, dimension_filters, evaluation_count, is_active, limit_amount, period_type, priority, rule_id, scope_id, scope_type, trigger_count

## public.expense_limit_evaluation -> mod_expenses.expense_policy_evaluations
- executable: False; mapped: 9; missing: ['batch_id']
- residue (6): context_data, evaluated_by_id, evaluation_id, period_spent_amount, result_message, rule_code

## public.expense_claim_action -> mod_expenses.expense_lifecycle_events
- executable: False; mapped: 3; missing: ['to_status', 'actor_reference', 'occurred_at']
- residue (4): action_id, action_key, action_type, status

## public.expense_entry -> mod_expenses.expense_requests
- executable: False; mapped: 7; missing: ['reference', 'requester_party_id', 'purpose', 'needed_by', 'total_requested_amount']
- residue (25): amount, approved_at, approved_by, business_unit_id, cost_center_id, created_by, description, expense_account_id, expense_date, expense_id, expense_number, journal_entry_id, payee, payment_account_id, payment_method, posted_at, posted_by, project_id, receipt_reference, submitted_by, task_id, tax_amount, tax_code_id, ticket_id ...

## public.purchase_requisition -> mod_procurement.purchase_requisitions
- executable: True; mapped: 9; missing: none
- residue (10): approval_request_id, approved_by_user_id, budget_verified, budget_verified_at, budget_verified_by_id, created_by_user_id, department_id, material_request_id, plan_item_id, requisition_id

## public.purchase_requisition_line -> mod_procurement.purchase_requisition_lines
- executable: True; mapped: 9; missing: none
- residue (4): delivery_date, expense_account_id, line_id, project_id

## public.request_for_quotation -> mod_procurement.sourcing_events
- executable: False; mapped: 3; missing: ['event_number', 'method', 'opens_at', 'closes_at', 'criteria_json', 'estimated_amount']
- residue (11): closing_date, created_by_user_id, estimated_value, evaluation_criteria, plan_item_id, procurement_method, requisition_id, rfq_date, rfq_id, rfq_number, terms_and_conditions

## public.rfq_invitation -> mod_procurement.sourcing_invitations
- executable: False; mapped: 2; missing: ['event_id']
- residue (4): invitation_id, responded, response_date, rfq_id

## public.quotation_response -> mod_procurement.bid_submissions
- executable: False; mapped: 9; missing: ['event_id', 'source_event_id']
- residue (4): notes, response_date, response_id, rfq_id

## public.quotation_response_line -> mod_procurement.bid_lines
- executable: False; mapped: 4; missing: ['event_id', 'bid_id', 'sourcing_line_id', 'line_total']
- residue (5): delivery_date, line_amount, line_id, requisition_line_id, response_id

## public.bid_evaluation -> mod_procurement.bid_evaluations
- executable: False; mapped: 2; missing: ['event_id', 'selected_bid_id', 'scores_json', 'selected_total_score', 'evaluated_at']
- residue (9): approval_request_id, approved_by_user_id, evaluated_by_user_id, evaluation_date, evaluation_id, evaluation_report, recommended_response_id, recommended_supplier_id, rfq_id

## public.purchase_order -> mod_procurement.purchase_orders
- executable: True; mapped: 14; missing: none
- residue (14): amendment_reason, amendment_version, approval_request_id, approved_by_user_id, budget_id, commitment_journal_entry_id, correlation_id, created_by_user_id, exchange_rate, is_amendment, original_po_id, po_id, shipping_address, variation_id

## public.purchase_order_line -> mod_procurement.purchase_order_lines
- executable: False; mapped: 10; missing: ['order_id', 'unit']
- residue (9): asset_account_id, delivery_date, expense_account_id, line_id, po_id, project_id, quantity_invoiced, segment_id, tax_code_id

## public.goods_receipt -> mod_procurement.receipt_observations
- executable: False; mapped: 2; missing: ['order_id', 'source_event_id', 'observed_at', 'lines_json']
- residue (9): notes, po_id, receipt_date, receipt_id, receipt_number, received_by_user_id, status, supplier_id, warehouse_id

## public.approval_request -> mod_approvals.approval_requests
- executable: False; mapped: 3; missing: ['policy_code', 'policy_version', 'subject_type', 'subject_id', 'content_digest', 'requested_by', 'state', 'idempotency_key']
- residue (13): correlation_id, document_amount, document_currency_code, document_id, document_reference, document_type, final_approver_user_id, notes, request_id, requested_at, requested_by_user_id, status, workflow_id

## public.approval_decision -> mod_approvals.approval_decisions
- executable: False; mapped: 5; missing: ['actor_id']
- residue (5): approver_user_id, comments, decision_id, delegated_from_user_id, ip_address

## public.approval_workflow -> mod_approvals.approval_policies
- executable: False; mapped: 3; missing: ['policy_code', 'version', 'levels', 'document_digest']
- residue (9): approval_levels, description, document_type, is_active, threshold_amount, threshold_currency_code, workflow_code, workflow_id, workflow_name

## public.asset -> mod_assets.assets
- executable: False; mapped: 10; missing: ['code', 'kind', 'state', 'condition']
- residue (36): accumulated_depreciation, acquisition_cost, acquisition_date, asset_number, barcode, cash_generating_unit_id, category_id, cost_center_id, created_by_user_id, currency_code, current_depreciation_schedule_id, custodian_employee_id, depreciation_method, depreciation_start_date, disposal_date, disposal_gain_loss, disposal_proceeds, functional_currency_cost, impairment_loss, in_service_date, insurance_policy_number, insured_value, invoice_reference, is_component_parent ...

## public.asset_assignment -> mod_assets.asset_assignments
- executable: False; mapped: 8; missing: ['custodian_id', 'starts_on']
- residue (6): assignment_id, employee_id, expected_return_date, issued_on, returned_on, transfer_from_assignment_id

## public.asset_disposal -> mod_assets.asset_disposals
- executable: False; mapped: 3; missing: ['method', 'status', 'requested_on', 'reason']
- residue (22): accumulated_depreciation_at_disposal, approved_by_user_id, authorization_reference, buyer_name, buyer_reference, cost_at_disposal, costs_of_disposal, created_by_user_id, disposal_date, disposal_id, disposal_proceeds, disposal_reason, disposal_type, fiscal_period_id, gain_loss_on_disposal, insurance_claim_reference, insurance_proceeds, invoice_number, journal_entry_id, net_book_value_at_disposal, net_proceeds, trade_in_asset_id

## public.asset_lifecycle_event -> mod_assets.asset_lifecycle_events
- executable: False; mapped: 7; missing: ['occurred_at']
- residue (12): actor_user_id, event_at, event_category, event_id, event_payload, new_owner_employee_id, new_status, previous_owner_employee_id, previous_status, source_record_id, source_type, updated_at

## public.asset_tracking_event -> mod_assets.asset_maintenance
- executable: False; mapped: 5; missing: ['kind', 'summary', 'status', 'scheduled_for']
- residue (11): accuracy_meters, latitude, location_id, longitude, movement_logged, previous_location_id, scanned_by_user_id, tracked_at, tracking_event_id, tracking_method, tracking_reference

## public.depreciation_run -> mod_finance.depreciation_runs
- executable: False; mapped: 6; missing: ['period_ref', 'through_date', 'currency_code', 'calculated_at']
- residue (10): calculation_completed_at, calculation_started_at, created_at, created_by_user_id, fiscal_period_id, journal_entry_id, posted_by_user_id, posting_batch_id, run_description, run_number

## public.depreciation_schedule -> mod_finance.depreciation_lines
- executable: False; mapped: 6; missing: ['book_id', 'book_version', 'periods', 'carrying_amount_opening', 'carrying_amount_closing', 'unimpaired_carrying_opening', 'unimpaired_depreciation_amount', 'unimpaired_carrying_closing', 'remaining_life_opening', 'remaining_life_closing']
- residue (10): accumulated_depreciation_closing, accumulated_depreciation_opening, asset_id, component_id, cost_basis, net_book_value_closing, net_book_value_opening, remaining_life_months_closing, remaining_life_months_opening, schedule_id

## GENERATED / command-import destinations
- mod_accounting.journal_line_dimensions: GENERATED: dimension rows split from legacy line columns; period events from fiscal period state
- mod_accounting.posted_ledger_dimensions: GENERATED: dimension rows split from legacy line columns; period events from fiscal period state
- mod_accounting.period_events: GENERATED: dimension rows split from legacy line columns; period events from fiscal period state
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
- mod_finance.asset_books: GENERATED: books enrol per asset; consequences are balanced outputs of impairment/revaluation/depreciation events
- mod_finance.accounting_events: GENERATED: books enrol per asset; consequences are balanced outputs of impairment/revaluation/depreciation events
- mod_finance.accounting_consequences: GENERATED: books enrol per asset; consequences are balanced outputs of impairment/revaluation/depreciation events
- mod_finance.accounting_consequence_lines: GENERATED: books enrol per asset; consequences are balanced outputs of impairment/revaluation/depreciation events

## Unpaired module destinations
none
