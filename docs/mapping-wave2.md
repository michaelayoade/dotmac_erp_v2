# Wave-2 transform specs — people, payroll, tax

Generated from `src/erp_v2/importer/wave2.py` pairings against the
real legacy model census and released module models. `missing` =
required destination columns with no source/constant (spec not
executable until ruled); `residue` = legacy columns no destination
consumes (e2/adapter/retire by rehearsal ruling).

## public.department -> mod_people.departments
- executable: True; mapped: 9; missing: none
- residue (2): cost_center_id, head_id

## public.designation -> mod_people.designations
- executable: True; mapped: 8; missing: none
- residue (1): ncc_staff_category

## public.employment_type -> mod_people.employment_types
- executable: True; mapped: 8; missing: none
- residue (0): none

## public.employee -> mod_people.employees
- executable: True; mapped: 14; missing: none
- residue (46): assigned_location_id, bank_account_name, bank_account_number, bank_branch_code, bank_name, batch_operation_id, blood_group, cost_center_id, ctc, current_accommodation_type, current_address, date_of_birth, default_payroll_payable_account_id, default_shift_type_id, dotmac_sub_access_enabled, dotmac_sub_account_id, dotmac_sub_roles, dotmac_sub_staff_synced_at, eligible_for_final_payroll, emergency_contact_name, emergency_contact_phone, expense_approver_id, final_payroll_cutoff_date, final_payroll_processed_at, gender, grade_id, mailcow_activated_at, mailcow_activation_expires_at, mailcow_activation_sent_at, mailcow_activation_token_hash, mailcow_mailbox_provisioned_at, mailcow_provisioning_requested_at, marital_status, nationality, notes, nysc_end_date, nysc_start_date, passport_number, passport_valid_upto, permanent_address, personal_email, personal_phone, reports_to_id, salary_mode, workforce_provisioning_state, workforce_provisioning_status

## public.position -> mod_people.positions
- executable: True; mapped: 11; missing: none
- residue (1): is_vacant

## public.position_assignment -> mod_people.position_assignments
- executable: True; mapped: 9; missing: none
- residue (0): none

## public.salary_component -> mod_payroll.pay_components
- executable: True; mapped: 9; missing: none
- residue (10): abbr, component_id, depends_on_payment_days, display_order, do_not_include_in_total, exempted_from_income_tax, is_active, is_statutory, is_tax_applicable, statistical_component

## public.salary_structure -> mod_payroll.pay_structures
- executable: False; mapped: 6; missing: ['minor_units']
- residue (4): description, is_active, payroll_frequency, structure_id

## public.salary_structure_earning -> mod_payroll.pay_structure_rules
- executable: False; mapped: 3; missing: ['tenant_id', 'component_code', 'component_name', 'component_kind', 'calculation_method', 'prorates']
- residue (5): amount, amount_based_on_formula, condition, earning_id, formula

## public.salary_structure_deduction -> mod_payroll.pay_structure_rules
- executable: False; mapped: 3; missing: ['tenant_id', 'component_code', 'component_name', 'component_kind', 'calculation_method', 'prorates']
- residue (5): amount, amount_based_on_formula, condition, deduction_id, formula

## public.salary_structure_assignment -> mod_payroll.employee_pay_assignments
- executable: True; mapped: 7; missing: none
- residue (4): assignment_id, base, income_tax_slab, variable

## public.payroll_entry -> mod_payroll.payroll_runs
- executable: False; mapped: 7; missing: ['minor_units', 'created_by_id']
- residue (28): department_id, designation_id, employee_count, employment_type_id, entry_id, entry_name, exchange_rate, expense_account_id, journal_entry_id, notes, payroll_frequency, payroll_month, payroll_year, payslips_email_error_count, payslips_email_last_run_at, payslips_email_processed_count, payslips_email_queued_at, payslips_email_queued_by_id, payslips_email_status, payslips_email_total_count, posting_date, salary_slips_created, salary_slips_submitted, source_bank_account_id, total_deductions, total_gross_pay, total_net_pay, updated_at

## public.salary_slip -> mod_payroll.payroll_calculations
- executable: False; mapped: 2; missing: ['run_id', 'assignment_id', 'revision_id', 'revision_version', 'proration_factor', 'gross_amount', 'deduction_amount', 'net_amount', 'employer_liability_amount', 'calculated_at']
- residue (42): absent_days, amount_paid, balance_due, bank_account_name, bank_account_number, bank_branch_code, bank_name, cost_center_id, created_at, currency_code, employee_name, end_date, exchange_rate, gross_pay, gross_pay_functional, is_auto_generated, journal_entry_id, leave_without_pay, needs_review, net_pay, net_pay_functional, notes, paid_at, paid_by_id, payment_days, payment_reference, payroll_entry_id, posted_at, posted_by_id, posting_date, review_reasons, reviewed_at, reviewed_by_id, slip_id, slip_number, start_date, status, structure_id, total_deduction, total_deduction_functional, total_working_days, updated_at

## public.salary_slip_earning -> mod_payroll.payroll_calculation_lines
- executable: False; mapped: 5; missing: ['tenant_id', 'component_code', 'component_kind', 'calculation_method', 'evidence_ref']
- residue (7): abbr, additional_amount, default_amount, do_not_include_in_total, line_id, statistical_component, year_to_date

## public.salary_slip_deduction -> mod_payroll.payroll_calculation_lines
- executable: False; mapped: 5; missing: ['tenant_id', 'component_code', 'component_kind', 'calculation_method', 'evidence_ref']
- residue (7): abbr, additional_amount, default_amount, do_not_include_in_total, line_id, statistical_component, year_to_date

## public.tax_jurisdiction -> mod_tax.tax_jurisdictions
- executable: False; mapped: 8; missing: ['authority_id', 'minor_units']
- residue (21): current_tax_expense_account_id, current_tax_payable_account_id, current_tax_rate, deferred_tax_asset_account_id, deferred_tax_expense_account_id, deferred_tax_liability_account_id, description, extension_months, filing_due_months, fiscal_year_end_month, future_rate_effective_from, future_tax_rate, has_reduced_rate, is_active, jurisdiction_id, jurisdiction_level, reduced_rate, reduced_rate_threshold, tax_authority_name, tax_id_number, tax_rate_effective_from

## public.tax_code -> mod_tax.tax_codes
- executable: True; mapped: 9; missing: none
- residue (16): applies_to_purchases, applies_to_sales, effective_from, effective_to, is_active, is_compound, is_fixed_amount, is_inclusive, is_recoverable, recovery_rate, reporting_code, tax_collected_account_id, tax_expense_account_id, tax_paid_account_id, tax_rate, tax_return_box

## public.tax_band -> mod_tax.tax_rule_bands
- executable: False; mapped: 6; missing: ['rule_id']
- residue (6): created_at, effective_from, effective_to, is_active, name, updated_at

## public.tax_return -> mod_tax.tax_returns
- executable: False; mapped: 7; missing: ['report_id', 'obligation_id', 'report_amount', 'adjustment_amount', 'payable_amount', 'currency_code', 'minor_units', 'created_by_id']
- residue (22): adjustments, box_values, filed_by_user_id, filed_date, final_amount, is_amendment, is_paid, jurisdiction_id, net_tax_payable, payment_date, payment_journal_entry_id, payment_reference, prepared_by_user_id, return_id, return_reference, return_type, reviewed_at, reviewed_by_user_id, tax_period_id, total_input_tax, total_output_tax, updated_at

## GENERATED destinations (no row copy)
- mod_payroll.pay_structure_revisions: GENERATED: one published v1 revision per structure at enrolment
- mod_payroll.pay_rule_bases: GENERATED: derived from rule calculation bases at enrolment
- mod_payroll.payroll_liabilities: GENERATED by payroll service from calculations; open legacy balances enter as enrolment liabilities with evidence refs
- mod_payroll.payroll_liability_settlements: GENERATED post-flip; legacy settlements stay calculation evidence
- mod_tax.tax_authorities: GENERATED: one authority per legacy jurisdiction level at enrolment
- mod_tax.tax_rules: GENERATED: legacy rate/effective columns re-publish as v1 rules through the tax service (policy is re-issued, not row-copied)
- mod_tax.statutory_report_boxes: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.statutory_report_definitions: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.statutory_report_values: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.statutory_reports: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_determination_dependencies: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_determination_lines: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_determination_prior_sets: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_determination_sets: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_determinations: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_filing_obligations: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_return_events: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_rule_dependency_codes: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_rule_supersessions: GENERATED by tax service from re-published policy (see tax_rules)
- mod_tax.tax_subject_classifications: GENERATED by tax service from re-published policy (see tax_rules)

## Module destinations with no pairing yet
none
