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
        note="file_id resolves through the files import (legacy file_path -> stored file)",
    ),
    Pairing(
        "hr_grievances",
        "public.grievance",
        renames={"number": "grievance_number"},
    ),
)

__all__ = ["E2_PAIRINGS"]
