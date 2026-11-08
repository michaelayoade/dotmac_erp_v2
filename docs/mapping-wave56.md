# Wave-5/6 transform specs - inventory, projects, satellites

Generated from `src/erp_v2/importer/waves36.py` against the real
legacy census and released module models. Same reading rules as mapping-wave2.md.

## public.item -> mod_inventory.items
- executable: True; mapped: 13; missing: none
- residue (28): average_cost, barcode, category_id, cogs_account_id, default_supplier_id, inventory_account_id, is_purchaseable, is_saleable, is_taxable, item_type, last_purchase_cost, lead_time_days, list_price, manufacturer_part_number, maximum_stock, minimum_stock, purchase_uom, reorder_point, reorder_quantity, revenue_account_id, sales_uom, tax_code_id, track_inventory, track_serial_numbers ...

## public.warehouse -> mod_inventory.warehouses
- executable: True; mapped: 8; missing: none
- residue (10): address, contact_email, contact_name, contact_phone, cost_center_id, is_consignment, is_receiving, is_shipping, is_transit, location_id

## public.inventory_lot -> mod_inventory.lots
- executable: False; mapped: 10; missing: ['status']
- residue (7): allocation_reference, certificate_of_analysis, initial_quantity, is_active, lot_id, purchase_order_id, supplier_id

## public.inventory_lot_balance -> mod_inventory.lot_balances
- executable: True; mapped: 6; missing: none
- residue (7): is_active, is_quarantined, lot_balance_id, qc_status, quantity_allocated, quantity_available, quarantine_reason

## public.inventory_serial -> mod_inventory.serials
- executable: True; mapped: 8; missing: none
- residue (4): is_active, location_id, notes, serial_id

## public.inventory_transaction -> mod_inventory.stock_movements
- executable: False; mapped: 15; missing: ['value_after']
- residue (12): fiscal_period_id, journal_entry_id, location_id, quantity_before, reason_code, reference, source_document_id, source_document_line_id, source_document_type, to_location_id, to_warehouse_id, uom

## public.inventory_serial_movement -> mod_inventory.movement_serials
- executable: True; mapped: 4; missing: none
- residue (9): created_by_user_id, from_location_id, from_warehouse_id, lot_id, movement_type, reason, to_location_id, to_warehouse_id, transaction_id

## public.stock_reservation -> mod_inventory.stock_reservations
- executable: True; mapped: 12; missing: none
- residue (7): priority, quantity_cancelled, reserved_at, reserved_by_user_id, source_id, source_line_id, source_type

## public.inventory_valuation -> mod_inventory.valuation_snapshots
- executable: True; mapped: 15; missing: none
- residue (7): estimated_costs_to_complete, estimated_selling_costs, estimated_selling_price, fiscal_period_id, functional_currency_amount, uom, write_down_journal_entry_id

## public.project -> mod_projects.projects
- executable: True; mapped: 9; missing: none
- residue (15): actual_cost, budget_amount, budget_currency_code, business_unit_id, cost_center_id, customer_id, end_date, estimated_cost, is_capitalizable, percent_complete, project_manager_user_id, project_template_id, project_type, segment_id, start_date

## public.project_template -> mod_projects.project_templates
- executable: True; mapped: 4; missing: none
- residue (2): project_type, template_id

## public.project_template_task -> mod_projects.project_template_tasks
- executable: True; mapped: 5; missing: none
- residue (1): template_task_id

## public.project_template_task_dependency -> mod_projects.project_template_task_dependencies
- executable: True; mapped: 5; missing: none
- residue (1): dependency_id

## public.milestone -> mod_projects.project_tasks
- executable: True; mapped: 8; missing: none
- residue (4): actual_date, linked_task_id, milestone_code, target_date

## public.ticket -> mod_tkt.tickets
- executable: True; mapped: 10; missing: none
- residue (12): category_id, contact_address, contact_email, contact_phone, customer_id, opening_date, project_id, raised_by_email, raised_by_id, resolution, resolution_date, team_id

## public.ticket_comment -> mod_tkt.ticket_comments
- executable: True; mapped: 6; missing: none
- residue (6): action, comment_id, comment_type, is_active, new_value, old_value

## public.hr_document -> mod_documents.documents
- executable: False; mapped: 9; missing: ['library_id', 'type_code', 'type_version', 'folder_path', 'sensitivity', 'handling_instructions']
- residue (17): acknowledgment_deadline_days, applies_to_all_employees, applies_to_departments, category, content_hash, content_type, description, document_id, effective_date, expiry_date, file_name, file_path, file_size_bytes, previous_version_id, requires_acknowledgment, updated_by, version

## public.form -> mod_forms.forms
- executable: True; mapped: 7; missing: none
- residue (2): owner_entity_id, owner_entity_type

## public.form_version -> mod_forms.form_versions
- executable: True; mapped: 7; missing: none
- residue (1): settings_json

## public.form_section -> mod_forms.form_sections
- executable: True; mapped: 4; missing: none
- residue (1): section_id

## public.form_field -> mod_forms.form_fields
- executable: True; mapped: 10; missing: none
- residue (6): field_id, is_filterable, placeholder, show_in_list, system_mapping, visibility_json

## public.form_field_option -> mod_forms.form_field_options
- executable: True; mapped: 5; missing: none
- residue (1): option_id

## public.form_submission -> mod_forms.form_submissions
- executable: True; mapped: 5; missing: none
- residue (3): subject_type, submission_id, submitted_by_email

## public.form_answer -> mod_forms.form_answers
- executable: True; mapped: 7; missing: none
- residue (4): answer_id, created_at, file_name, file_url

## public.workflow_execution -> mod_workflow.workflow_executions
- executable: True; mapped: 6; missing: none
- residue (11): duration_ms, entity_type, error_details, error_message, max_retries, result, retry_count, trigger_data, trigger_event, triggered_at, triggered_by

## GENERATED / command-import destinations
- mod_inventory.stock_balances: GENERATED: rebuildable projection from movements (parity-checked, never imported)
- mod_projects.project_task_dependencies: GENERATED/split from legacy task rows at rehearsal
- mod_projects.project_task_assignees: GENERATED/split from legacy task rows at rehearsal
- mod_analytics.metric_catalog_entries: GENERATED: metrics re-declared; observations rebuilt from authoritative module rows post-flip
- mod_analytics.metric_ingest_receipts: GENERATED: metrics re-declared; observations rebuilt from authoritative module rows post-flip
- mod_analytics.metric_observations: GENERATED: metrics re-declared; observations rebuilt from authoritative module rows post-flip
- mod_analytics.metric_points: GENERATED: metrics re-declared; observations rebuilt from authoritative module rows post-flip
- mod_analytics.metric_projection_rebuilds: GENERATED: metrics re-declared; observations rebuilt from authoritative module rows post-flip
- mod_aiops.ai_policies: GENERATED: coach prompts/context are e2 product code over the advisory protocol
- mod_aiops.ai_policy_versions: GENERATED: coach prompts/context are e2 product code over the advisory protocol
- mod_aiops.ai_operations: GENERATED: coach prompts/context are e2 product code over the advisory protocol
- mod_aiops.ai_execution_attempts: GENERATED: coach prompts/context are e2 product code over the advisory protocol
- mod_aiops.ai_insights: GENERATED: coach prompts/context are e2 product code over the advisory protocol
- mod_documents.document_libraries: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_type_versions: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_versions: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_renditions: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_classifications: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_relations: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_checkouts: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_annotations: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_access_grants: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_acknowledgements: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_documents.document_events: GENERATED/enrolment: libraries+types are config; versions/renditions derive from files storage
- mod_inbox.conversations: GENERATED post-flip: legacy email_delivery is kernel-outbox evidence, not inbox state
- mod_inbox.messages: GENERATED post-flip: legacy email_delivery is kernel-outbox evidence, not inbox state
- mod_inbox.conversation_read_states: GENERATED post-flip: legacy email_delivery is kernel-outbox evidence, not inbox state
- mod_inbox.message_transport_refs: GENERATED post-flip: legacy email_delivery is kernel-outbox evidence, not inbox state
- mod_timers.timers: GENERATED post-flip: timers are live scheduling state, re-armed by owners at the flip
- mod_timers.timer_acceptances: GENERATED post-flip: timers are live scheduling state, re-armed by owners at the flip
- mod_timers.timer_rejections: GENERATED post-flip: timers are live scheduling state, re-armed by owners at the flip
- mod_workflow.workflow_checkpoints: GENERATED: checkpoints only for executions live at the flip (drain-first ruling)
- mod_workflow.workflow_repairs: GENERATED: checkpoints only for executions live at the flip (drain-first ruling)

## Unpaired module destinations
none
