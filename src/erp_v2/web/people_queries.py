"""People workspace reads: module + e2 rows shaped for templates.

Read-only presentation queries over dotmac-people models and the
e2 leave tables. All writes stay with the owning services; nothing
here mutates. Every query is tenant-filtered explicitly — RLS is the
backstop, not the filter.
"""

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.models import PartyPerson
from dotmac_people.models import Department, Designation, Employee
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.hr.models import LeaveAllocation, LeaveApplication, LeaveType


def list_employees(db: Session, *, tenant_id: UUID, limit: int = 100) -> list[dict[str, object]]:
    rows = db.execute(
        select(
            Employee.employee_code,
            PartyPerson.first_name,
            PartyPerson.last_name,
            Department.name.label("department"),
            Designation.name.label("designation"),
            Employee.status,
            Employee.date_of_joining,
        )
        .join(PartyPerson, PartyPerson.party_id == Employee.party_id)
        .outerjoin(
            Department,
            (Department.tenant_id == Employee.tenant_id) & (Department.id == Employee.department_id),
        )
        .outerjoin(
            Designation,
            (Designation.tenant_id == Employee.tenant_id) & (Designation.id == Employee.designation_id),
        )
        .where(Employee.tenant_id == tenant_id)
        .order_by(Employee.employee_code)
        .limit(limit)
    ).mappings()
    return [
        {
            "code": r["employee_code"],
            "name": f"{r['first_name']} {r['last_name']}".strip(),
            "department": r["department"] or "—",
            "designation": r["designation"] or "—",
            "status": r["status"],
            "joined": r["date_of_joining"],
        }
        for r in rows
    ]


def leave_overview(db: Session, *, tenant_id: UUID, limit: int = 50) -> dict[str, object]:
    applications = db.execute(
        select(
            LeaveApplication.number,
            Employee.employee_code,
            LeaveType.name.label("leave_type"),
            LeaveApplication.from_date,
            LeaveApplication.to_date,
            LeaveApplication.total_leave_days,
            LeaveApplication.status,
        )
        .join(
            Employee,
            (Employee.tenant_id == LeaveApplication.tenant_id) & (Employee.id == LeaveApplication.employee_id),
        )
        .join(
            LeaveType,
            (LeaveType.tenant_id == LeaveApplication.tenant_id) & (LeaveType.id == LeaveApplication.leave_type_id),
        )
        .where(LeaveApplication.tenant_id == tenant_id)
        .order_by(LeaveApplication.from_date.desc())
        .limit(limit)
    ).mappings()
    allocations = db.scalars(
        select(LeaveAllocation)
        .where(
            LeaveAllocation.tenant_id == tenant_id,
            LeaveAllocation.is_active.is_(True),
        )
        .order_by(LeaveAllocation.from_date.desc())
        .limit(limit)
    )
    codes = {e.id: e.employee_code for e in db.scalars(select(Employee).where(Employee.tenant_id == tenant_id))}
    types = {t.id: t.name for t in db.scalars(select(LeaveType).where(LeaveType.tenant_id == tenant_id))}
    return {
        "applications": [
            {
                "number": r["number"],
                "employee": r["employee_code"],
                "leave_type": r["leave_type"],
                "from_date": r["from_date"],
                "to_date": r["to_date"],
                "days": r["total_leave_days"],
                "status": r["status"],
            }
            for r in applications
        ],
        "balances": [
            {
                "employee": codes.get(a.employee_id, "—"),
                "leave_type": types.get(a.leave_type_id, "—"),
                "allocated": a.new_leaves_allocated + a.carry_forward_leaves,
                "used": a.leaves_used,
                "balance": a.balance,
            }
            for a in allocations
        ],
    }


__all__ = ["leave_overview", "list_employees"]
