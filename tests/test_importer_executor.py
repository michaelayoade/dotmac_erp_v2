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
            DestinationColumn("minor_units", nullable=False, has_default=False),
        )
    )
    assert not spec.executable
    assert spec.missing_required == ("minor_units",)
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


def test_executor_refuses_non_executable_spec() -> None:
    src, dst = _engines()
    spec = _spec(destination_columns=(DestinationColumn("nope", nullable=False, has_default=False),))
    with src.connect() as s, dst.connect() as d, pytest.raises(ValueError):
        execute_transform(s, d, spec)
