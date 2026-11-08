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
)

__all__ = ["E2_PAIRINGS"]
