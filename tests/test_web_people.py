"""People workspace queries over module + e2 tables (tenant-filtered)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import dotmac_people.models as people_models
import sqlalchemy as sa
from dotmac_kernel.models import Base, Party, PartyPerson, PartyType, Tenant
from sqlalchemy.orm import Session

from erp_v2.hr import models as hr
from erp_v2.web.people_queries import leave_overview, list_employees


def _session_with_people():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_people"))
    tables = [
        Tenant.__table__,
        Party.__table__,
        PartyPerson.__table__,
        people_models.Department.__table__,
        people_models.Designation.__table__,
        people_models.EmploymentType.__table__,
        people_models.Employee.__table__,
    ] + [Base.metadata.tables[n] for n in hr.HR_TABLES]
    Base.metadata.create_all(engine, tables=tables)
    s = Session(engine)
    tenant_id, other_tenant = uuid.uuid4(), uuid.uuid4()
    s.add_all(
        [
            Tenant(id=tenant_id, slug="acme", name="Acme"),
            Tenant(id=other_tenant, slug="other", name="Other"),
        ]
    )
    dept = people_models.Department(tenant_id=tenant_id, code="ENG", name="Engineering")
    s.add(dept)
    s.flush()
    ids = {}
    for code, first, tid in (("E001", "Ada", tenant_id), ("E999", "Mallory", other_tenant)):
        party_id = uuid.uuid4()
        s.add(
            Party(
                id=party_id,
                tenant_id=tid,
                party_type=PartyType.person,
                display_name=first,
                email=f"{first.lower()}@x.io",
            )
        )
        s.add(PartyPerson(party_id=party_id, first_name=first, last_name="Test"))
        emp = people_models.Employee(
            tenant_id=tid,
            party_id=party_id,
            employee_code=code,
            department_id=dept.id if tid == tenant_id else None,
            date_of_joining=date(2025, 1, 1),
        )
        s.add(emp)
        ids[code] = emp
    s.flush()
    return s, tenant_id, ids


def test_list_employees_is_tenant_filtered_and_joined() -> None:
    s, tenant_id, _ = _session_with_people()
    rows = list_employees(s, tenant_id=tenant_id)
    assert [r["code"] for r in rows] == ["E001"]
    assert rows[0]["name"] == "Ada Test"
    assert rows[0]["department"] == "Engineering"


def test_leave_overview_balances_and_applications() -> None:
    s, tenant_id, ids = _session_with_people()
    lt = hr.LeaveType(tenant_id=tenant_id, code="ANNUAL", name="Annual")
    s.add(lt)
    s.flush()
    alloc = hr.LeaveAllocation(
        tenant_id=tenant_id,
        employee_id=ids["E001"].id,
        leave_type_id=lt.id,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 12, 31),
        new_leaves_allocated=Decimal(20),
        leaves_used=Decimal(5),
    )
    s.add(alloc)
    s.flush()
    s.add(
        hr.LeaveApplication(
            tenant_id=tenant_id,
            number="LV-1",
            employee_id=ids["E001"].id,
            leave_type_id=lt.id,
            allocation_id=alloc.id,
            from_date=date(2026, 2, 2),
            to_date=date(2026, 2, 6),
            total_leave_days=Decimal(5),
            status="approved",
        )
    )
    s.flush()
    overview = leave_overview(s, tenant_id=tenant_id)
    assert overview["balances"][0]["balance"] == Decimal(15)
    assert overview["applications"][0]["number"] == "LV-1"
    assert overview["applications"][0]["employee"] == "E001"


def test_templates_render_with_empty_and_full_context() -> None:
    # Template syntax smoke via Jinja directly (request-level proof
    # needs the PG test server).
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    from erp_v2 import ui

    root = Path(__file__).resolve().parents[1] / "src" / "erp_v2" / "templates"
    env = Environment(loader=FileSystemLoader(root), autoescape=True)
    ctx = {**ui.template_globals()}
    assert "No employees yet" in env.get_template("people/employees.html").render(employees=[], **ctx)
    html = env.get_template("people/leave.html").render(applications=[], balances=[], **ctx)
    assert "No active leave allocations" in html
    login = env.get_template("auth/login.html").render(next_url="/", error=None, **ctx)
    assert "Sign in to Dotmac ERP" in login
