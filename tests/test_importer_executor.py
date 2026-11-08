"""Spec + executor contract on synthetic legacy-shaped tables."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from erp_v2.importer.executor import execute_transform
from erp_v2.importer.spec import DestinationColumn, build_spec


def _spec(**overrides):
    kwargs = {
        "source_table": "department",
        "destination_table": "departments",
        "source_columns": (
            "department_id",
            "organization_id",
            "department_code",
            "department_name",
            "head_id",
        ),
        "destination_columns": (
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("tenant_id", nullable=False, has_default=False),
            DestinationColumn("code", nullable=False, has_default=False),
            DestinationColumn("name", nullable=False, has_default=False),
        ),
        "renames": {"code": "department_code", "name": "department_name"},
    }
    kwargs.update(overrides)
    return build_spec(**kwargs)


def test_spec_systematic_renames_and_residue() -> None:
    spec = _spec()
    assert spec.executable
    assert spec.column_map["id"] == "department_id"
    assert spec.column_map["tenant_id"] == "organization_id"
    assert spec.residue == ("head_id",)


def test_spec_missing_required_is_named_not_filled() -> None:
    spec = _spec(
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("no_such_thing", nullable=False, has_default=False),
        )
    )
    assert not spec.executable
    assert spec.missing_required == ("no_such_thing",)
    # minor_units is run-supplied instead: spec builds, executor refuses
    # without the run value (covered in test_run_supplied_and_computed_columns).
    run_spec = _spec(
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("minor_units", nullable=False, has_default=False),
        )
    )
    assert run_spec.run_supplied == ("minor_units",)
    const_spec = _spec(
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("minor_units", nullable=False, has_default=False),
        ),
        constants={"minor_units": 2},
    )
    assert const_spec.executable


def _engines():
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE department (department_id TEXT, organization_id TEXT,"
                " department_code TEXT, department_name TEXT, head_id TEXT)"
            )
        )
        c.execute(sa.text("INSERT INTO department VALUES ('d1','t1','FIN','Finance','e9')"))
        c.execute(sa.text("INSERT INTO department VALUES ('d2','tX','OPS','Operations',NULL)"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE departments (id TEXT, tenant_id TEXT, code TEXT, name TEXT)"))
    return src, dst


def test_executor_writes_rewrites_and_queues_exceptions() -> None:
    src, dst = _engines()
    spec = _spec(fk_rewrites={"tenant_id": "tenant"})
    with src.connect() as s, dst.begin() as d:
        dry = execute_transform(s, d, spec, id_maps={"tenant": {"t1": "T1"}}, dry_run=True)
        assert dry.read == 2 and dry.written == 1 and not dry.complete
        assert len(dry.exceptions) == 1
        assert dry.exceptions[0].source_key == "d2"
        assert d.execute(sa.text("SELECT count(*) FROM departments")).scalar() == 0
        report = execute_transform(s, d, spec, id_maps={"tenant": {"t1": "T1"}})
        assert report.written == 1
        row = d.execute(sa.text("SELECT id, tenant_id, code FROM departments")).one()
        assert tuple(row) == ("d1", "T1", "FIN")


def test_run_supplied_and_computed_columns() -> None:
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    spec = build_spec(
        source_table="journal_entry_line",
        destination_table="journal_lines",
        source_columns=("line_id", "entry_id", "account_id", "debit"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("tenant_id", nullable=False, has_default=False),
            DestinationColumn("journal_id", nullable=False, has_default=False),
            DestinationColumn("request_fingerprint", nullable=False, has_default=False),
            DestinationColumn("evidence_ref", nullable=False, has_default=False),
        ),
        renames={"id": "line_id", "journal_id": "entry_id"},
    )
    assert spec.executable
    assert spec.run_supplied == ("tenant_id",)
    assert spec.computed == {
        "request_fingerprint": "sha256_row",
        "evidence_ref": "source_key",
    }
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE journal_entry_line (line_id TEXT, entry_id TEXT, account_id TEXT, debit TEXT)"))
        c.execute(sa.text("INSERT INTO journal_entry_line VALUES ('l1','e1','a1','5')"))
    with dst.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE journal_lines (id TEXT, tenant_id TEXT, journal_id TEXT,"
                " request_fingerprint TEXT, evidence_ref TEXT)"
            )
        )
    with src.connect() as s, dst.begin() as d:
        with pytest.raises(ValueError, match="run-supplied"):
            execute_transform(s, d, spec)
        report = execute_transform(s, d, spec, run_constants={"tenant_id": "t1"})
        assert report.complete
        row = d.execute(sa.text("SELECT tenant_id, evidence_ref, length(request_fingerprint) FROM journal_lines")).one()
        assert tuple(row) == ("t1", "legacy:journal_entry_line:l1", 64)


def test_lookup_columns_resolve_or_exception() -> None:
    spec = build_spec(
        source_table="salary_structure_earning",
        destination_table="pay_structure_rules",
        source_columns=("earning_id", "component_id", "display_order"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("component_id", nullable=False, has_default=False),
            DestinationColumn("sequence", nullable=False, has_default=False),
            DestinationColumn("component_code", nullable=False, has_default=False),
        ),
        renames={"id": "earning_id", "sequence": "display_order"},
        lookups={"component_code": ("pay_component", "component_id", "code")},
    )
    assert spec.executable  # lookup covers the required column
    assert "component_code" not in spec.missing_required
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(
            sa.text("CREATE TABLE salary_structure_earning (earning_id TEXT, component_id TEXT, display_order INTEGER)")
        )
        c.execute(sa.text("INSERT INTO salary_structure_earning VALUES ('r1','c1',1)"))
        c.execute(sa.text("INSERT INTO salary_structure_earning VALUES ('r2','cX',2)"))
    with dst.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE pay_structure_rules (id TEXT, component_id TEXT, sequence INTEGER, component_code TEXT)"
            )
        )
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec, id_maps={"pay_component": {"c1": {"code": "BASIC"}}})
        assert report.written == 1 and not report.complete
        assert report.exceptions[0].column == "component_code"
        assert report.exceptions[0].source_key == "r2"
        row = d.execute(sa.text("SELECT id, component_code FROM pay_structure_rules")).one()
        assert tuple(row) == ("r1", "BASIC")


def test_lookup_key_must_be_mapped() -> None:
    with pytest.raises(ValueError, match="unmapped"):
        build_spec(
            source_table="salary_structure_earning",
            destination_table="pay_structure_rules",
            source_columns=("earning_id",),
            destination_columns=(
                DestinationColumn("id", nullable=False, has_default=False),
                DestinationColumn("component_code", nullable=False, has_default=False),
            ),
            lookups={"component_code": ("pay_component", "component_id", "code")},
        )


def test_slug_computed_and_digest_convention() -> None:
    spec = build_spec(
        source_table="project_template",
        destination_table="project_templates",
        source_columns=("template_id", "name"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("code", nullable=False, has_default=False),
            DestinationColumn("name", nullable=False, has_default=False),
            DestinationColumn("document_digest", nullable=False, has_default=False),
        ),
        renames={"id": "template_id"},
        computed={"code": "slug:name"},
    )
    assert spec.executable
    assert spec.computed == {"code": "slug:name", "document_digest": "sha256_row"}
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE project_template (template_id TEXT, name TEXT)"))
        c.execute(sa.text("INSERT INTO project_template VALUES ('t1','Site Rollout (Phase 2)')"))
        c.execute(sa.text("INSERT INTO project_template VALUES ('t2','!!!')"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE project_templates (id TEXT, code TEXT, name TEXT, document_digest TEXT)"))
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec)
        assert report.written == 1 and not report.complete
        assert report.exceptions[0].column == "code"
        row = d.execute(sa.text("SELECT code, length(document_digest) FROM project_templates")).one()
        assert tuple(row) == ("site-rollout-phase-2", 64)


def test_slug_source_must_be_mapped() -> None:
    with pytest.raises(ValueError, match="slug"):
        build_spec(
            source_table="project_template",
            destination_table="project_templates",
            source_columns=("template_id",),
            destination_columns=(
                DestinationColumn("id", nullable=False, has_default=False),
                DestinationColumn("code", nullable=False, has_default=False),
            ),
            renames={"id": "template_id"},
            computed={"code": "slug:name"},
        )


def test_executor_refuses_non_executable_spec() -> None:
    src, dst = _engines()
    spec = _spec(destination_columns=(DestinationColumn("nope", nullable=False, has_default=False),))
    with src.connect() as s, dst.connect() as d, pytest.raises(ValueError):
        execute_transform(s, d, spec)


def test_scalar_defaults_and_mul_and_uuid5_row() -> None:
    spec = build_spec(
        source_table="bank_directory",
        destination_table="bank_institutions",
        source_columns=("bank_code", "bank_name", "quantity", "unit_price"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("code", nullable=False, has_default=False),
            DestinationColumn("name", nullable=False, has_default=False),
            DestinationColumn("quantity", nullable=False, has_default=False),
            DestinationColumn("unit_price", nullable=False, has_default=False),
            DestinationColumn("gross", nullable=False, has_default=False),
            DestinationColumn("is_active", nullable=False, has_default=False, scalar_default=True),
        ),
        renames={"code": "bank_code", "name": "bank_name"},
        computed={"id": "uuid5_row", "gross": "mul:quantity:unit_price"},
    )
    assert spec.executable
    assert spec.defaults == {"is_active": True}
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(
            sa.text("CREATE TABLE bank_directory (bank_code TEXT, bank_name TEXT, quantity REAL, unit_price REAL)")
        )
        c.execute(sa.text("INSERT INTO bank_directory VALUES ('044','Access',3,2.5)"))
    with dst.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE bank_institutions (id TEXT, code TEXT, name TEXT, "
                "quantity REAL, unit_price REAL, gross REAL, is_active INTEGER)"
            )
        )
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec)
        assert report.complete
        row = d.execute(sa.text("SELECT length(id), code, gross, is_active FROM bank_institutions")).one()
        assert tuple(row) == (32, "044", 7.5, 1)


def test_id_preserved_via_last_token_systematic() -> None:
    spec = build_spec(
        source_table="salary_slip",
        destination_table="payroll_calculations",
        source_columns=("slip_id", "employee_id"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("employee_ref", nullable=False, has_default=False),
        ),
        renames={"employee_ref": "employee_id"},
    )
    assert spec.column_map["id"] == "slip_id"
    spec2 = build_spec(
        source_table="posted_ledger_line",
        destination_table="posted_ledger_lines",
        source_columns=("ledger_line_id", "debit"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("debit", nullable=False, has_default=False),
        ),
    )
    assert spec2.column_map["id"] == "ledger_line_id"


def test_updated_at_coalesce_and_per_row_integrity_exception() -> None:
    spec = build_spec(
        source_table="department",
        destination_table="departments",
        source_columns=("department_id", "name", "created_at", "updated_at"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("name", nullable=False, has_default=False),
            DestinationColumn("created_at", nullable=False, has_default=False),
            DestinationColumn("updated_at", nullable=False, has_default=False),
        ),
    )
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE department (department_id TEXT, name TEXT, created_at TEXT, updated_at TEXT)"))
        c.execute(sa.text("INSERT INTO department VALUES ('d1','Tech','2025-01-01',NULL)"))
        c.execute(sa.text("INSERT INTO department VALUES ('d2',NULL,'2025-01-01',NULL)"))  # name NULL -> rejected
        c.execute(sa.text("INSERT INTO department VALUES ('d3','Ops','2025-01-01','2025-02-01')"))
    with dst.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE departments (id TEXT, name TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
        )
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec)
        assert report.written == 2 and not report.complete
        assert len(report.exceptions) == 1
        assert report.exceptions[0].source_key == "d2"
        assert "destination rejected" in report.exceptions[0].reason
        rows = d.execute(sa.text("SELECT id, updated_at FROM departments ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [("d1", "2025-01-01"), ("d3", "2025-02-01")]
