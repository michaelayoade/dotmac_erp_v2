"""ERP-unique (`e2`) destinations and their legacy pairings.

e2 tables are this assembly's own lineage (public schema, branch
`assembly`); only ERP-unique domain lands there. Pairings here cover
each ported slice as it lands — first: HR leave.
"""

from __future__ import annotations

from typing import Final

from erp_v2.importer.wave2 import Pairing

E2_PAIRINGS: Final[tuple[Pairing, ...]] = (
    Pairing(
        "hr_leave_types",
        "public.leave_type",
        renames={"code": "leave_type_code", "name": "leave_type_name"},
        note="residue: allocation_policy/carry/encash thresholds re-ruled at HR-policy enrolment",
    ),
    Pairing(
        "hr_holiday_lists",
        "public.holiday_list",
        renames={"code": "list_code", "name": "list_name"},
        note="weekly_off format converts at rehearsal (legacy weekday names -> ISO numbers)",
    ),
    Pairing(
        "hr_holidays",
        "public.holiday",
        renames={"name": "holiday_name"},
        note="tenant_id derives from the parent list at rehearsal (legacy holiday has no org column)",
    ),
    Pairing("hr_leave_allocations", "public.leave_allocation"),
    Pairing(
        "hr_leave_applications",
        "public.leave_application",
        renames={"number": "application_number", "decided_by_id": "approved_by_id"},
        note="allocation_id resolved by employee+type+period match; status values "
        "lowercase-map at rehearsal; payroll posting flags are payroll-wave facts",
    ),
    # attendance slice
    Pairing(
        "hr_shift_types",
        "public.shift_type",
        renames={
            "code": "shift_code",
            "name": "shift_name",
            "late_entry_grace_minutes": "late_entry_grace_period",
            "early_exit_grace_minutes": "early_exit_grace_period",
        },
    ),
    Pairing("hr_shift_assignments", "public.shift_assignment"),
    Pairing(
        "hr_attendance",
        "public.attendance",
        renames={"marked_by_id": "marked_by"},
        note="late/early booleans derive from minutes at rehearsal; schedule refs are workforce residue",
    ),
    Pairing("hr_attendance_requests", "public.attendance_request"),
    # appraisals slice
    Pairing(
        "hr_appraisal_cycles",
        "public.appraisal_cycle",
        renames={"code": "cycle_code", "name": "cycle_name"},
    ),
    Pairing(
        "hr_kras",
        "public.kra",
        renames={"code": "kra_code", "name": "kra_name"},
        note="department/designation scoping is template residue",
    ),
    Pairing(
        "hr_appraisal_templates",
        "public.appraisal_template",
        renames={"code": "template_code", "name": "template_name"},
        note="pms_config/template_profile JSON is policy residue for the web slice",
    ),
    Pairing("hr_appraisal_template_kras", "public.appraisal_template_kra"),
    Pairing(
        "hr_appraisals",
        "public.appraisal",
        note="wide legacy row: core flow columns map; calibration/committee/quarterly/"
        "secondment detail is residue for the feedback/appeals slice",
    ),
    Pairing("hr_appraisal_kra_scores", "public.appraisal_kra_score"),
    # feedback + appeals slice
    Pairing("hr_appraisal_feedback", "public.appraisal_feedback"),
    Pairing(
        "hr_appraisal_appeals",
        "public.appraisal_appeal",
        note="mediation/committee detail collapses into status+notes at rehearsal",
    ),
    Pairing("hr_appraisal_outcome_actions", "public.appraisal_outcome_action"),
    # discipline slice
    Pairing(
        "hr_disciplinary_cases",
        "public.disciplinary_case",
        renames={"number": "case_number"},
    ),
    Pairing("hr_case_responses", "public.case_response"),
    Pairing("hr_case_witnesses", "public.case_witness"),
    Pairing("hr_case_actions", "public.case_action"),
    Pairing(
        "hr_case_documents",
        "public.case_document",
        lookups={"file_id": ("case_document_file", "id", "file_id")},
        note="file_id resolves through the files import: enrolment matches "
        "legacy file_path to mod_files.stored_files.storage_key exactly",
    ),
    Pairing(
        "hr_grievances",
        "public.grievance",
        renames={"number": "grievance_number"},
    ),
    # recruitment slice
    Pairing(
        "hr_job_openings",
        "public.job_opening",
        renames={"code": "job_code", "title": "job_title"},
        note="salary band/skills text is careers-surface residue; application_form links forms module",
    ),
    Pairing(
        "hr_job_applicants",
        "public.job_applicant",
        note="verification_token/resume_url are discarded for file-id + fresh verification at flip",
    ),
    Pairing("hr_interviews", "public.interview"),
    Pairing(
        "hr_job_offers",
        "public.job_offer",
        renames={"number": "offer_number"},
        note="candidate_access_token discarded; fresh self-service token issued post-flip",
    ),
    # lifecycle slice
    Pairing(
        "hr_checklist_templates",
        "public.checklist_template",
        renames={"code": "template_code", "name": "template_name"},
    ),
    Pairing(
        "hr_checklist_template_items",
        "public.checklist_template_item",
        renames={"name": "item_name"},
        note="tenant derives from parent template (legacy item has no org column)",
    ),
    Pairing(
        "hr_employee_onboardings",
        "public.employee_onboarding",
        note="progress_percentage dropped (derived); self-service token discarded",
    ),
    Pairing(
        "hr_onboarding_activities",
        "public.employee_onboarding_activity",
        renames={"name": "activity_name", "document_file_id": "document_id"},
        note="two status columns collapse to one at rehearsal; tenant from parent onboarding",
    ),
    Pairing(
        "hr_employee_separations",
        "public.employee_separation",
        renames={"exit_interview_notes": "exit_interview"},
    ),
    Pairing("hr_clearance_items", "public.clearance_item"),
    Pairing(
        "hr_employment_changes",
        "public.employee_promotion",
        renames={"change_date": "promotion_date"},
        constants={"change_type": "promotion"},
        note="also fed by employee_transfer with change_type=transfer (second spec)",
    ),
    Pairing(
        "hr_employment_changes",
        "public.employee_transfer",
        renames={"change_date": "transfer_date"},
        constants={"change_type": "transfer"},
    ),
    Pairing(
        "hr_employment_change_details",
        "public.employee_promotion_detail",
        renames={"change_id": "promotion_id"},
        note="also fed by employee_transfer_detail (change_id: transfer_id)",
    ),
    # training + credentials slice
    Pairing(
        "hr_training_programs",
        "public.training_program",
        renames={"code": "program_code", "name": "program_name"},
    ),
    Pairing(
        "hr_training_events",
        "public.training_event",
        renames={"name": "event_name"},
    ),
    Pairing("hr_training_attendees", "public.training_attendee"),
    Pairing(
        "hr_qualifications",
        "public.employee_qualification",
        renames={"name": "qualification_name", "document_file_id": "document_id"},
    ),
    Pairing(
        "hr_certifications",
        "public.employee_certification",
        renames={"name": "certification_name", "document_file_id": "document_id"},
    ),
    Pairing(
        "hr_competencies",
        "public.competency",
        renames={"code": "competency_code", "name": "competency_name"},
        note="level_1..5 descriptions are rubric residue for the web slice",
    ),
    Pairing(
        "hr_skills",
        "public.skill",
        renames={"name": "skill_name"},
    ),
    Pairing("hr_employee_skills", "public.employee_skill"),
)

FLEET_PAIRINGS: Final[tuple[Pairing, ...]] = (
    Pairing(
        "fleet_vehicle_profiles",
        "public.vehicle",
        renames={"code": "vehicle_code", "gps_device_ref": "gps_device_id"},
        lookups={"asset_id": ("vehicle_asset", "id", "asset_id")},
        note="asset_id resolves only on an exact VIN <-> asset serial_number "
        "match at enrolment; unmatched vehicles exception for manual "
        "asset linkage (never fuzzy-matched)",
    ),
    Pairing("fleet_fuel_logs", "public.fuel_log_entry", renames={"profile_id": "vehicle_id"}),
    Pairing("fleet_reservations", "public.vehicle_reservation", renames={"profile_id": "vehicle_id"}),
    Pairing("fleet_incidents", "public.vehicle_incident", renames={"profile_id": "vehicle_id"}),
    Pairing("fleet_maintenance_records", "public.maintenance_record", renames={"profile_id": "vehicle_id"}),
    Pairing(
        "fleet_vehicle_documents",
        "public.vehicle_document",
        renames={"profile_id": "vehicle_id"},
        lookups={"file_id": ("vehicle_document_file", "id", "file_id")},
        note="file_id resolves through the files import: enrolment matches "
        "legacy file_path to mod_files.stored_files.storage_key exactly",
    ),
)

PUBLIC_SECTOR_PAIRINGS: Final[tuple[Pairing, ...]] = (
    Pairing("ps_funds", "public.fund", renames={"code": "fund_code", "name": "fund_name"}),
    Pairing(
        "ps_appropriations",
        "public.appropriation",
        renames={
            "code": "appropriation_code",
            "name": "appropriation_name",
            "fiscal_year_ref": "fiscal_year_id",
            "account_ref": "account_id",
        },
    ),
    Pairing(
        "ps_commitments",
        "public.commitment",
        renames={"number": "commitment_number"},
        note="journal refs and allotment/dimension detail are rehearsal residue",
    ),
    Pairing("ps_commitment_lines", "public.commitment_line", renames={"account_ref": "account_id"}),
    Pairing(
        "ps_virements",
        "public.virement",
        renames={"number": "virement_number"},
        note="account/cost-center granular virements collapse to appropriation-level at rehearsal",
    ),
)

LEASE_PAIRINGS: Final[tuple[Pairing, ...]] = (
    Pairing(
        "lease_contracts",
        "public.lease_contract",
        renames={
            "number": "lease_number",
            "name": "lease_name",
            "lessor_ref": "lessor_supplier_id",
            "discount_rate": "discount_rate_used",
        },
        note="options/index-linkage terms are enrolment residue; schedules regenerate and reconcile",
    ),
    Pairing(
        "lease_liabilities",
        "public.lease_liability",
        renames={"current_balance": "current_liability_balance"},
        note="PV component split and current/non-current portions derive at rehearsal",
    ),
    Pairing(
        "lease_rou_assets",
        "public.lease_asset",
        renames={"initial_value": "initial_rou_asset_value"},
    ),
    Pairing(
        "lease_payment_schedule",
        "public.lease_payment_schedule",
        renames={
            "opening_balance": "opening_liability_balance",
            "closing_balance": "closing_liability_balance",
        },
        note="regenerated schedules must reconcile row-for-row; paid rows import as paid evidence",
    ),
)

# Consolidation (consolidation_run/elimination_entry/consolidated_*)
# and coach: no e2 tables by design. Consolidation is cross-entity
# reporting over accounting balances (platform/reporting concern on
# top of dotmac-accounting, ruled when the group-reporting surface is
# designed); coach consumes dotmac-ai-operations with ERP prompts in
# the web slice. Both stay census-visible in SOURCE_TABLES.

# Deliberately NOT paired: the legacy in-ERP course LMS
# (training_course*, training_lesson*, training_question*,
# training_assessment*, training_exam_*) overlaps Academy's
# dotmac-learning ownership. ERP consumes learning state through the
# Academy boundary (academy_learning_progress/requirement stay
# adapter observations in the census); rebuilding an LMS inside ERP
# would recreate the duplication the composition removes.

__all__ = ["E2_PAIRINGS", "FLEET_PAIRINGS", "LEASE_PAIRINGS", "PUBLIC_SECTOR_PAIRINGS"]
