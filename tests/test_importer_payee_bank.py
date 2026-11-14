"""Legacy payee bank details land as PROPOSED (imported-pending-approval), never approved."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa

from erp_v2.importer.minting import MINT_STEPS, MintContext, run_mint_step

UTC = timezone.utc
TENANT = "00000000-0000-4000-8000-000000000001"
OTHER_TENANT = "00000000-0000-4000-8000-000000000002"
EMP1, EMP2, EMP3, EMP_NOBANK = (f"10000000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3, 4))
SUP1, SUP2, SUP3, SUP4 = (f"20000000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3, 4))
CTX = MintContext(tenant_id=TENANT, actor_id="a", occurred_at=datetime(2026, 11, 14, tzinfo=UTC))
SECRET_NUMBERS = ("0123456789", "1111111111", "2222222222", "3333333333")


def _conn(ddl):
    conn = sa.create_engine("sqlite://").connect()
    for schema in ("public", "mod_people"):
        conn.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    for statement in ddl:
        conn.execute(sa.text(statement))
    return conn


def _insert(conn, table, columns, rows):
    marks = ", ".join(f":{c}" for c in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"  # noqa: S608
    conn.execute(sa.text(sql), [dict(zip(columns, r, strict=True)) for r in rows])


def _source():
    sup = json.dumps(
        {"bank_code": "058", "bank_name": "Example Bank", "account_name": "SUP ONE", "account_number": "1111111111"}
    )
    last4 = json.dumps({"bank_code": "058", "bank_name": "Example Bank", "account_number_last4": "2222"})
    conn = _conn(
        (
            "CREATE TABLE public.employee (employee_id TEXT, organization_id TEXT, employee_code TEXT, bank_name TEXT, "
            "bank_account_number TEXT, bank_account_name TEXT, bank_branch_code TEXT)",
            "CREATE TABLE public.supplier (supplier_id TEXT, organization_id TEXT, legal_name TEXT, trading_name TEXT, "
            "bank_details TEXT)",
        )
    )
    _insert(
        conn,
        "public.employee",
        (
            "employee_id",
            "organization_id",
            "employee_code",
            "bank_name",
            "bank_account_number",
            "bank_account_name",
            "bank_branch_code",
        ),
        [
            (EMP1, TENANT, "E-001", "Example Bank", "0123456789", "A PERSON", "044"),
            (EMP2, TENANT, "E-002", "Example Bank", "2222222222", "B PERSON", None),
            (EMP3, OTHER_TENANT, "E-003", "Example Bank", "3333333333", "C", None),
            (EMP_NOBANK, TENANT, "E-004", None, "  ", None, None),
        ],
    )
    _insert(
        conn,
        "public.supplier",
        ("supplier_id", "organization_id", "legal_name", "trading_name", "bank_details"),
        [
            (SUP1, TENANT, "Acme Ltd", "Acme", sup),
            (SUP2, TENANT, None, "Trade Only", last4),
            (SUP3, TENANT, "No Bank", None, None),
            (SUP4, OTHER_TENANT, "Elsewhere", None, sup),
        ],
    )
    return conn


def _dest(imported=(EMP1,)):
    conn = _conn(
        [
            "CREATE TABLE mod_people.employees (id TEXT, tenant_id TEXT)",
            "CREATE TABLE public.payee_bank_instructions (id TEXT, tenant_id TEXT, payee_kind TEXT, payee_ref TEXT, "
            "payee_label TEXT, bank_code TEXT, bank_name TEXT, account_number TEXT, account_last4 TEXT, "
            "account_name TEXT, status TEXT, origin TEXT, evidence_ref TEXT, proposed_by_party_id TEXT, "
            "proposed_at TEXT, created_at TEXT, updated_at TEXT)",
        ]
    )
    _insert(conn, "mod_people.employees", ("id", "tenant_id"), [(e, TENANT) for e in imported])
    return conn


def _rows(dst):
    return {
        (r["payee_kind"], r["payee_ref"]): r
        for r in dst.execute(sa.text("SELECT * FROM public.payee_bank_instructions")).mappings()
    }


def test_step_is_registered():
    assert "payee_bank_instructions" in dict(MINT_STEPS)


def test_mint_creates_only_proposed_legacy_import_rows_for_this_tenant():
    src, dst = _source(), _dest()
    result = run_mint_step("payee_bank_instructions", src, dst, CTX)
    assert result.refused is None and result.created == 2 and result.existing == 0
    rows = _rows(dst)
    assert set(rows) == {("employee", EMP1), ("supplier", SUP1)}
    for row in rows.values():
        assert (row["status"], row["origin"], row["proposed_by_party_id"]) == ("proposed", "legacy_import", None)
        assert row["evidence_ref"].startswith("legacy-import:")
    employee, supplier = rows[("employee", EMP1)], rows[("supplier", SUP1)]
    assert employee["bank_code"] is None  # legacy employee has a branch code, never a bank code
    assert employee["account_last4"] == "6789" and employee["payee_label"] == "E-001"
    assert (supplier["bank_code"], supplier["payee_label"], supplier["account_name"]) == ("058", "Acme Ltd", "SUP ONE")


def test_unimported_employee_and_last4_only_supplier_are_not_minted_and_blockers_hold_no_numbers():
    src, dst = _source(), _dest()
    result = run_mint_step("payee_bank_instructions", src, dst, CTX)
    assert any("not yet imported" in b for b in result.blockers)  # EMP2
    assert (("employee", EMP2) not in _rows(dst)) and (("supplier", SUP2) not in _rows(dst))
    blob = repr(result)
    assert not any(n in blob for n in SECRET_NUMBERS)


def test_rerun_is_idempotent_and_never_overwrites_an_existing_live_instruction():
    src, dst = _source(), _dest()
    run_mint_step("payee_bank_instructions", src, dst, CTX)
    dst.execute(sa.text("UPDATE public.payee_bank_instructions SET status = 'approved'"))
    again = run_mint_step("payee_bank_instructions", src, dst, CTX)
    assert (again.created, again.existing) == (0, 2)
    assert {r["status"] for r in _rows(dst).values()} == {"approved"}


def test_missing_destination_table_is_a_recorded_refusal_not_a_crash():
    src = _source()
    dst = _conn(["CREATE TABLE mod_people.employees (id TEXT, tenant_id TEXT)"])
    result = run_mint_step("payee_bank_instructions", src, dst, CTX)
    assert result.refused and "database error" in result.refused and result.created == 0


def test_over_long_legacy_field_is_a_blocker_by_id_only():
    src = _source()
    src.execute(
        sa.text("UPDATE public.employee SET bank_name = :n WHERE employee_id = :e"), {"n": "x" * 200, "e": EMP1}
    )
    dst = _dest()
    result = run_mint_step("payee_bank_instructions", src, dst, CTX)
    assert ("employee", EMP1) not in _rows(dst)
    assert any(EMP1 in b and "over-long" in b for b in result.blockers)
