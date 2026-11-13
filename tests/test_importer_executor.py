"""Spec + executor contract on synthetic legacy-shaped tables."""

from __future__ import annotations

import hashlib
from decimal import Decimal

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
        row_hash_columns=("request_fingerprint",),
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


def test_slug_computed_and_declared_digest() -> None:
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
        row_hash_columns=("document_digest",),
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


def test_commit_per_spec_persists_each_table(tmp_path) -> None:
    from erp_v2.importer.pipeline import run_import

    def _spec(src: str, dst: str):
        return build_spec(
            source_table=src,
            destination_table=dst,
            source_columns=(f"{src}_id", "name"),
            destination_columns=(
                DestinationColumn("id", nullable=False, has_default=False),
                DestinationColumn("name", nullable=False, has_default=False),
            ),
            renames={"id": f"{src}_id"},
        )

    src = sa.create_engine("sqlite://")
    dest_path = tmp_path / "dest.db"
    dst = sa.create_engine(f"sqlite:///{dest_path}")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE alpha (alpha_id TEXT, name TEXT)"))
        c.execute(sa.text("INSERT INTO alpha VALUES ('a1','A')"))
        c.execute(sa.text("CREATE TABLE beta (beta_id TEXT, name TEXT)"))
        c.execute(sa.text("INSERT INTO beta VALUES ('b1','B')"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE alphas (id TEXT, name TEXT)"))
        c.execute(sa.text("CREATE TABLE betas (id TEXT, name TEXT)"))
    with src.connect() as s, dst.connect() as d:
        receipt = run_import(s, d, (_spec("alpha", "alphas"), _spec("beta", "betas")), commit_per_spec=True)
        assert receipt.complete
        # No explicit commit of the second spec by the caller: close
        # without committing - per-spec commits already persisted.
        d.rollback()
    with dst.connect() as d:
        assert d.execute(sa.text("SELECT count(*) FROM alphas")).scalar() == 1
        assert d.execute(sa.text("SELECT count(*) FROM betas")).scalar() == 1


def test_order_specs_parents_before_children() -> None:
    from erp_v2.importer.pipeline import order_specs

    meta = sa.MetaData()
    parties = sa.Table("parties", meta, sa.Column("id", sa.String, primary_key=True), schema="mod")
    employees = sa.Table(
        "employees",
        meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("party_id", sa.String, sa.ForeignKey("mod.parties.id")),
        schema="mod",
    )
    tables = {"mod.parties": parties, "mod.employees": employees}

    def _spec(dst: str):
        return build_spec(
            source_table=dst,
            destination_table=dst,
            source_columns=("id",),
            destination_columns=(DestinationColumn("id", nullable=False, has_default=False),),
        )

    ordered = order_specs((_spec("mod.employees"), _spec("mod.parties")), tables)
    assert [s.destination_table for s in ordered] == ["mod.parties", "mod.employees"]


def test_fk_rejected_rows_retry_after_parent_lands() -> None:
    # Self-FK tree arriving child-first: the retry pass lands the
    # child once its parent (later in the same spec) is written.
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")

    @sa.event.listens_for(dst, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE nodes (node_id TEXT, parent_id TEXT)"))
        c.execute(sa.text("INSERT INTO nodes VALUES ('child','root'), ('root',NULL), ('orphan','ghost')"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE nodes_out (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES nodes_out(id))"))
    spec = build_spec(
        source_table="nodes",
        destination_table="nodes_out",
        source_columns=("node_id", "parent_id"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("parent_id", nullable=True, has_default=False),
        ),
        renames={"id": "node_id"},
    )
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec)
    assert report.read == 3
    assert report.written == 2  # root + child (via retry)
    assert len(report.exceptions) == 1
    assert report.exceptions[0].source_key == "orphan"
    assert "FOREIGN KEY" in report.exceptions[0].reason


def test_partition_parent_blocked_skips_children_of_unrun_parents() -> None:
    from erp_v2.importer.pipeline import partition_parent_blocked

    meta = sa.MetaData()
    parents = sa.Table("parents", meta, sa.Column("id", sa.String, primary_key=True), schema="mod")
    children = sa.Table(
        "children",
        meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("parent_id", sa.String, sa.ForeignKey("mod.parents.id")),
        schema="mod",
    )
    free = sa.Table("free", meta, sa.Column("id", sa.String, primary_key=True), schema="mod")
    tables = {"mod.parents": parents, "mod.children": children, "mod.free": free}

    def _spec(dst: str):
        return build_spec(
            source_table=dst,
            destination_table=dst,
            source_columns=("id",),
            destination_columns=(DestinationColumn("id", nullable=False, has_default=False),),
        )

    all_specs = (_spec("mod.parents"), _spec("mod.children"), _spec("mod.free"))
    runnable = (_spec("mod.children"), _spec("mod.free"))  # parents spec not running
    kept, skipped = partition_parent_blocked(all_specs, runnable, tables)
    assert [s.destination_table for s in kept] == ["mod.free"]
    assert [s.destination_table for s in skipped] == ["mod.children"]
    # When the parent runs, nothing is partitioned off.
    kept2, skipped2 = partition_parent_blocked(all_specs, all_specs, tables)
    assert len(kept2) == 3 and not skipped2


def test_null_fallbacks_row_id_literal_and_run() -> None:
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE docs (doc_id TEXT, ext_id TEXT, kind TEXT, actor TEXT)"))
        c.execute(sa.text("INSERT INTO docs VALUES ('d1', NULL, NULL, NULL), ('d2', 'X9', 'INVOICE', 'u7')"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE docs_out (id TEXT, ext_id TEXT, kind TEXT, actor TEXT)"))
    spec = build_spec(
        source_table="docs",
        destination_table="docs_out",
        source_columns=("doc_id", "ext_id", "kind", "actor"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("ext_id", nullable=False, has_default=False),
            DestinationColumn("kind", nullable=False, has_default=False),
            DestinationColumn("actor", nullable=False, has_default=False),
        ),
        renames={"id": "doc_id"},
        null_fallbacks={"ext_id": "row_id", "kind": "literal:JOURNAL", "actor": "run:actor"},
    )
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec, run_constants={"actor": "sys"})
    assert report.written == 2 and report.complete
    with dst.connect() as d:
        rows = d.execute(sa.text("SELECT id, ext_id, kind, actor FROM docs_out ORDER BY id")).all()
    assert rows == [("d1", "d1", "JOURNAL", "sys"), ("d2", "X9", "INVOICE", "u7")]


def test_deferred_self_fk_columns_apply_after_inserts() -> None:
    # Child-first self-FK rows: deferred parent links apply in the
    # post-pass, so no FK retry churn and no exceptions.
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")

    @sa.event.listens_for(dst, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE nodes (node_id TEXT, parent_id TEXT)"))
        c.execute(sa.text("INSERT INTO nodes VALUES ('child','root'), ('root',NULL)"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE nodes_out (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES nodes_out(id))"))
    spec = build_spec(
        source_table="nodes",
        destination_table="nodes_out",
        source_columns=("node_id", "parent_id"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("parent_id", nullable=True, has_default=False),
        ),
        renames={"id": "node_id"},
        deferred=("parent_id",),
    )
    with src.connect() as s_, dst.begin() as d_:
        report = execute_transform(s_, d_, spec)
    assert report.written == 2 and report.complete
    with dst.connect() as d_:
        rows = dict(d_.execute(sa.text("SELECT id, parent_id FROM nodes_out")).all())
    assert rows == {"child": "root", "root": None}


def test_data_error_rows_become_exceptions_not_crashes() -> None:
    # A type-invalid value (text into an integer column) raises
    # DataError, not IntegrityError; it must land in the exception
    # queue, never kill the run (Gate 4: hr_interviews.round labels).
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE vals (val_id TEXT, amount TEXT)"))
        c.execute(sa.text("INSERT INTO vals VALUES ('v1','10'), ('v2','HR_ROUND'), ('v3','30')"))
    with dst.begin() as c:
        # STRICT table: sqlite enforces the INTEGER type.
        c.execute(sa.text("CREATE TABLE vals_out (id TEXT, amount INTEGER) STRICT"))
    spec = build_spec(
        source_table="vals",
        destination_table="vals_out",
        source_columns=("val_id", "amount"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("amount", nullable=False, has_default=False),
        ),
        renames={"id": "val_id"},
    )
    with src.connect() as s_, dst.begin() as d_:
        report = execute_transform(s_, d_, spec)
    assert report.written == 2
    assert [e.source_key for e in report.exceptions] == ["v2"]


def test_fk_guard_prevalidates_parents_without_db_churn() -> None:
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")

    @sa.event.listens_for(dst, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE lines (line_id TEXT, head_id TEXT)"))
        c.execute(sa.text("INSERT INTO lines VALUES ('l1','h1'), ('l2','ghost'), ('l3','h1')"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE heads (id TEXT PRIMARY KEY)"))
        c.execute(sa.text("INSERT INTO heads VALUES ('h1')"))
        c.execute(sa.text("CREATE TABLE lines_out (id TEXT PRIMARY KEY, head_id TEXT REFERENCES heads(id))"))
    spec = build_spec(
        source_table="lines",
        destination_table="lines_out",
        source_columns=("line_id", "head_id"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("head_id", nullable=False, has_default=False),
        ),
        renames={"id": "line_id"},
    )
    with src.connect() as s_, dst.begin() as d_:
        report = execute_transform(s_, d_, spec)
    assert report.written == 2
    assert len(report.exceptions) == 1
    exc = report.exceptions[0]
    assert exc.source_key == "l2" and exc.column == "head_id"
    assert "foreign key parent absent" in exc.reason


def test_null_fallback_column_copies_mapped_value() -> None:
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE led (led_id TEXT, debit REAL, orig_debit REAL)"))
        c.execute(sa.text("INSERT INTO led VALUES ('a', 10.5, NULL), ('b', 3.0, 2.5)"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE led_out (id TEXT, debit REAL, orig_debit REAL)"))
    spec = build_spec(
        source_table="led",
        destination_table="led_out",
        source_columns=("led_id", "debit", "orig_debit"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("debit", nullable=False, has_default=False),
            DestinationColumn("orig_debit", nullable=False, has_default=False),
        ),
        renames={"id": "led_id"},
        null_fallbacks={"orig_debit": "column:debit"},
    )
    with src.connect() as s_, dst.begin() as d_:
        report = execute_transform(s_, d_, spec)
    assert report.written == 2 and report.complete
    with dst.connect() as d_:
        rows = d_.execute(sa.text("SELECT id, orig_debit FROM led_out ORDER BY id")).all()
    assert rows == [("a", 10.5), ("b", 2.5)]


def test_dict_values_serialize_for_json_columns() -> None:
    # psycopg returns JSONB as dict; raw text() inserts cannot adapt
    # dicts - the executor serializes them (Gate 4: mod_forms).
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE forms (form_id TEXT)"))
        c.execute(sa.text("INSERT INTO forms VALUES ('f1')"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE forms_out (id TEXT, settings TEXT)"))
    spec = build_spec(
        source_table="forms",
        destination_table="forms_out",
        source_columns=("form_id",),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("settings", nullable=False, has_default=False),
        ),
        renames={"id": "form_id"},
        lookups={"settings": ("form_settings", "id", "settings")},
    )
    with src.connect() as s_, dst.begin() as d_:
        report = execute_transform(s_, d_, spec, id_maps={"form_settings": {"f1": {"settings": {"a": 1}}}})
    assert report.written == 1 and report.complete
    with dst.connect() as d_:
        stored = d_.execute(sa.text("SELECT settings FROM forms_out")).scalar_one()
    assert stored == '{"a": 1}'


def test_json_columns_encode_scalar_answers() -> None:
    src = sa.create_engine("sqlite://")
    dst = sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE answers (answer_id TEXT, value_json TEXT)"))
        c.execute(sa.text("INSERT INTO answers VALUES ('a1','plain text'), ('a2','42'), ('a3',NULL)"))
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE answers_out (id TEXT, value_json TEXT)"))
    spec = build_spec(
        source_table="answers",
        destination_table="answers_out",
        source_columns=("answer_id", "value_json"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("value_json", nullable=True, has_default=False),
        ),
        renames={"id": "answer_id"},
        json_columns=("value_json",),
    )
    with src.connect() as s_, dst.begin() as d_:
        report = execute_transform(s_, d_, spec)
    assert report.written == 3
    with dst.connect() as d_:
        rows = d_.execute(sa.text("SELECT id, value_json FROM answers_out ORDER BY id")).all()
    assert rows == [("a1", '"plain text"'), ("a2", '"42"'), ("a3", None)]


@pytest.mark.parametrize("column", ("receipt_evidence_fingerprint", "content_sha256", "evidence_digest"))
@pytest.mark.parametrize("nullable", (False, True))
def test_unsourced_hash_names_do_not_manufacture_proof(column: str, nullable: bool) -> None:
    spec = build_spec(
        source_table="legacy_item",
        destination_table="items",
        source_columns=("id",),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn(column, nullable=nullable, has_default=False),
        ),
    )
    assert column not in spec.column_map
    assert column not in spec.computed
    assert column not in spec.defaults
    assert spec.missing_required == (() if nullable else (column,))
    assert spec.executable is nullable


@pytest.mark.parametrize("source_name", ("request_fingerprint", "recorded_hash"))
@pytest.mark.parametrize("value", (None, "source-recorded-fingerprint"))
def test_declared_row_hash_preserves_source_value_including_null(source_name: str, value: str | None) -> None:
    spec = build_spec(
        source_table="legacy_item",
        destination_table="items",
        source_columns=("id", source_name),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("request_fingerprint", nullable=True, has_default=False),
        ),
        renames={"request_fingerprint": source_name} if source_name != "request_fingerprint" else {},
        row_hash_columns=("request_fingerprint",),
    )
    assert spec.column_map["request_fingerprint"] == source_name
    assert spec.computed == {}
    src, dst = sa.create_engine("sqlite://"), sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text(f"CREATE TABLE legacy_item (id TEXT, {source_name} TEXT)"))
        c.execute(sa.text("INSERT INTO legacy_item VALUES ('r1', :value)"), {"value": value})
    with dst.begin() as c:
        c.execute(sa.text("CREATE TABLE items (id TEXT, request_fingerprint TEXT)"))
    with src.connect() as source, dst.begin() as target:
        assert execute_transform(source, target, spec).complete
        assert target.execute(sa.text("SELECT request_fingerprint FROM items")).scalar_one() == value


@pytest.mark.parametrize(
    ("declaration", "message"),
    (
        (("unsafe;column",), "unsafe SQL identifier"),
        (("absent",), "absent from destination"),
        (("request_fingerprint", "request_fingerprint"), "duplicate row hash"),
    ),
)
def test_row_hash_declarations_are_validated(declaration: tuple[str, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_spec(
            source_table="legacy_item",
            destination_table="items",
            source_columns=("id",),
            destination_columns=(DestinationColumn("request_fingerprint", nullable=False, has_default=False),),
            row_hash_columns=declaration,
        )


def test_declared_hashes_keep_exact_bytes_and_computed_order() -> None:
    spec = build_spec(
        source_table="legacy_item",
        destination_table="items",
        source_columns=("id", "name", "amount"),
        destination_columns=tuple(
            DestinationColumn(name, nullable=False, has_default=False)
            for name in (
                "id",
                "name",
                "amount",
                "source_fingerprint",
                "evidence_ref",
                "request_fingerprint",
                "code",
                "submission_key",
            )
        ),
        # Declaration order must not reorder the destination-column walk.
        row_hash_columns=("request_fingerprint", "source_fingerprint"),
        computed={"code": "slug:name", "submission_key": "source_key"},
    )
    assert list(spec.computed.items()) == [
        ("source_fingerprint", "sha256_row"),
        ("evidence_ref", "source_key"),
        ("request_fingerprint", "sha256_row"),
        ("code", "slug:name"),
        ("submission_key", "source_key"),
    ]
    src, dst = sa.create_engine("sqlite://"), sa.create_engine("sqlite://")
    with src.begin() as c:
        c.execute(sa.text("CREATE TABLE legacy_item (id TEXT, name TEXT, amount TEXT)"))
        c.execute(sa.text("INSERT INTO legacy_item VALUES ('r1', 'Site Rollout', '12345678901234567890.1200')"))
    with dst.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE items (id TEXT, name TEXT, amount TEXT, source_fingerprint TEXT, "
                "evidence_ref TEXT, request_fingerprint TEXT, code TEXT, submission_key TEXT)"
            )
        )
    first_bytes = b"[('amount', '12345678901234567890.1200'), ('id', 'r1'), ('name', 'Site Rollout')]"
    first_hash = hashlib.sha256(first_bytes).hexdigest()
    second_bytes = (
        "[('amount', '12345678901234567890.1200'), ('evidence_ref', 'legacy:legacy_item:r1'), "
        "('id', 'r1'), ('name', 'Site Rollout'), ('source_fingerprint', '" + first_hash + "')]"
    ).encode()
    with src.connect() as source, dst.begin() as target:
        assert execute_transform(source, target, spec).complete
        row = target.execute(sa.text("SELECT * FROM items")).mappings().one()
        assert row["source_fingerprint"] == first_hash
        assert row["request_fingerprint"] == hashlib.sha256(second_bytes).hexdigest()
        assert row["code"] == "site-rollout"
        assert row["submission_key"] == row["evidence_ref"] == "legacy:legacy_item:r1"
        assert row["amount"] == "12345678901234567890.1200"
        assert Decimal(row["amount"]).as_tuple() == Decimal("12345678901234567890.1200").as_tuple()


def test_explicit_computed_still_overrides_source_and_fallback() -> None:
    spec = build_spec(
        source_table="legacy_item",
        destination_table="items",
        source_columns=("id", "request_fingerprint"),
        destination_columns=(DestinationColumn("request_fingerprint", nullable=False, has_default=False),),
        row_hash_columns=("request_fingerprint",),
        computed={"request_fingerprint": "source_key"},
    )
    assert spec.column_map == {}
    assert spec.computed == {"request_fingerprint": "source_key"}
