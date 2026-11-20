"""B-13 history cutoff: closed years reach v2 only through the opening; unposted WHT is queued by default."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa

from erp_v2.importer import cli
from erp_v2.importer import history_cutoff as hc
from erp_v2.importer.executor import execute_transform
from erp_v2.importer.spec import DestinationColumn, build_spec
from erp_v2.importer.waves36 import ALL_WAVE_PAIRINGS, WAVE3_PAIRINGS
from erp_v2.migration_ledger.register import IMPORT_CLASS

CUTOFF = date(2025, 12, 31)


def col(name):
    return DestinationColumn(name, nullable=False, has_default=False)


def journal_spec(**overrides):
    kwargs = {
        "source_table": "journal_entry",
        "destination_table": "journal_entries",
        "source_columns": ("journal_entry_id", "posting_date", "entry_date", "status"),
        "destination_columns": (col("id"), col("posting_date"), col("entry_date"), col("status")),
        "history_cutoff": hc.KIND_JOURNAL,
    }
    kwargs.update(overrides)
    return build_spec(**kwargs)


def line_spec():
    return build_spec(
        source_table="journal_entry_line",
        destination_table="journal_lines",
        source_columns=("line_id", "journal_entry_id", "debit"),
        destination_columns=(col("id"), col("journal_id"), col("debit")),
        renames={"journal_id": "journal_entry_id"},
        history_cutoff=hc.KIND_JOURNAL_LINE,
    )


def ledger_spec():
    return build_spec(
        source_table="posted_ledger_line",
        destination_table="posted_ledger_lines",
        source_columns=("ledger_line_id", "journal_entry_id", "posting_date", "debit_amount", "credit_amount"),
        destination_columns=(col("id"), col("journal_id"), col("posting_date"), col("debit")),
        renames={"journal_id": "journal_entry_id", "debit": "debit_amount"},
        history_cutoff=hc.KIND_LEDGER_LINE,
    )


def seed_source(engine):
    with engine.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE journal_entry (journal_entry_id TEXT, posting_date TEXT, entry_date TEXT, status TEXT)"
            )
        )
        c.execute(sa.text("CREATE TABLE journal_entry_line (line_id TEXT, journal_entry_id TEXT, debit TEXT)"))
        c.execute(
            sa.text(
                "CREATE TABLE posted_ledger_line "
                "(ledger_line_id TEXT, journal_entry_id TEXT, posting_date TEXT, debit_amount TEXT, credit_amount TEXT)"
            )
        )
        for jid, posting, entry in (
            ("j-old", "2025-12-31", "2025-12-31"),  # ON the cutoff: excluded
            ("j-older", None, "2025-06-01"),  # posting_date NULL: entry_date decides
            ("j-new", "2026-01-01", "2026-01-01"),
            ("j-undated", None, None),  # not provably pre-cutoff: lands, counted
        ):
            c.execute(
                sa.text("INSERT INTO journal_entry VALUES (:j, :p, :e, 'POSTED')"), {"j": jid, "p": posting, "e": entry}
            )
            c.execute(sa.text("INSERT INTO journal_entry_line VALUES (:l, :j, '1')"), {"l": "l-" + jid, "j": jid})
            for side, debit, credit in (("d", "1", "0"), ("c", "0", "1")):
                c.execute(
                    sa.text("INSERT INTO posted_ledger_line VALUES (:l, :j, :p, :d, :c)"),
                    {"l": f"pl-{jid}-{side}", "j": jid, "p": posting or entry, "d": debit, "c": credit},
                )
        # P-5: an unbalanced journal after the cutoff (6-dp residue), refused as a unit
        c.execute(sa.text("INSERT INTO journal_entry VALUES ('j-unb', '2026-02-01', '2026-02-01', 'POSTED')"))
        c.execute(sa.text("INSERT INTO journal_entry_line VALUES ('l-j-unb', 'j-unb', '1')"))
        c.execute(
            sa.text("INSERT INTO posted_ledger_line VALUES ('pl-j-unb-d', 'j-unb', '2026-02-01', '1.000001', '0')")
        )
        c.execute(sa.text("INSERT INTO posted_ledger_line VALUES ('pl-j-unb-c', 'j-unb', '2026-02-01', '0', '1')"))
        # a pre-cutoff DRAFT journal: excluded, and itemised by legacy status
        c.execute(sa.text("INSERT INTO journal_entry VALUES ('j-draft', '2025-02-02', '2025-02-02', 'DRAFT')"))
        # an orphan line (no parent journal row) is never silently dropped by the cutoff
        c.execute(sa.text("INSERT INTO journal_entry_line VALUES ('l-orphan', 'j-missing', '1')"))


def dest_engine(table, columns):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text(f"CREATE TABLE {table} ({', '.join(columns)})"))
    return engine


def run(spec, table, columns, **kwargs):
    src = sa.create_engine("sqlite://")
    seed_source(src)
    dst = dest_engine(table, columns)
    with src.connect() as s, dst.begin() as d:
        report = execute_transform(s, d, spec, **kwargs)
        landed = sorted(r[0] for r in d.execute(sa.text(f"SELECT id FROM {table}")))  # noqa: S608
    return report, landed


# --- the executor ---------------------------------------------------------------------------------------------


def test_a_cutoff_spec_refuses_to_run_without_the_cutoff():
    src = sa.create_engine("sqlite://")
    seed_source(src)
    dst = dest_engine("journal_entries", ["id", "posting_date", "entry_date", "status"])
    with src.connect() as s, dst.begin() as d, pytest.raises(hc.HistoryCutoffRefused) as exc:
        execute_transform(s, d, journal_spec())
    assert exc.value.code == hc.REFUSAL_REQUIRED


def test_journals_on_or_before_the_cutoff_are_not_landed_and_are_counted_with_a_key_digest():
    report, landed = run(
        journal_spec(), "journal_entries", ["id", "posting_date", "entry_date", "status"], history_cutoff=CUTOFF
    )
    assert landed == ["j-new"]  # the undated journal is a named refusal, the unbalanced one a unit refusal
    assert report.history_cutoff == "2025-12-31" and report.history_cutoff_source == 6
    assert report.history_cutoff_excluded == 3 and report.history_cutoff_undated == 1
    assert report.history_cutoff_excluded_sha256 == hc.keys_sha256(["j-draft", "j-old", "j-older"])  # key order
    assert report.history_cutoff_excluded_by_status == {"POSTED": 2, "DRAFT": 1}
    assert report.history_cutoff_nonposted_keys == {"DRAFT": ["j-draft"]}
    ((unit, debit, credit),) = report.history_cutoff_unit_refused_journals
    assert (unit, Decimal(debit), Decimal(credit)) == ("j-unb", Decimal("1.000001"), Decimal(1))
    assert [(e.source_key, e.reason) for e in report.exceptions] == [("j-undated", hc.REFUSAL_UNDATED)]
    assert report.read == 1 and report.written == 1 and not report.complete


def test_journal_lines_follow_their_parent_and_an_orphan_line_is_never_silently_dropped():
    report, landed = run(line_spec(), "journal_lines", ["id", "journal_id", "debit"], history_cutoff=CUTOFF)
    # the orphan line is read; it fails only if the destination refuses it, it is not excluded by the cutoff
    assert landed == ["l-j-new", "l-orphan"]  # the undated parent's line is refused; j-unb's line is a unit refusal
    assert report.history_cutoff_excluded == 2 and report.history_cutoff_undated == 1
    assert report.history_cutoff_unit_refused == 1


def test_ledger_lines_are_filtered_on_their_own_posting_date():
    report, landed = run(
        ledger_spec(), "posted_ledger_lines", ["id", "journal_id", "posting_date", "debit"], history_cutoff=CUTOFF
    )
    # undated ledger lines are named refusals; the unbalanced journal's lines are a unit refusal
    assert landed == ["pl-j-new-c", "pl-j-new-d"]
    assert report.history_cutoff_excluded == 4 and report.history_cutoff_undated == 2
    assert report.history_cutoff_unit_refused == 2


def test_the_cutoff_is_a_bound_parameter_not_sql_text():
    _report, landed = run(
        journal_spec(),
        "journal_entries",
        ["id", "posting_date", "entry_date", "status"],
        history_cutoff=date(2026, 1, 1),
    )
    assert landed == ["j-unb"] or landed == []  # the unbalanced journal is a unit refusal
    assert "2026" not in hc.exclusion_predicate(hc.KIND_JOURNAL, "journal_entry", None)


# --- the spec -------------------------------------------------------------------------------------------------


def test_a_cutoff_spec_whose_source_lacks_the_date_column_is_not_executable():
    spec = journal_spec(source_columns=("journal_entry_id", "status"), destination_columns=(col("id"), col("status")))
    assert not spec.executable and hc.MISSING_COLUMN in spec.missing_required


def test_an_unknown_cutoff_kind_is_refused_at_build_time():
    with pytest.raises(ValueError):
        journal_spec(history_cutoff="everything")


def test_exactly_the_three_gl_pairings_declare_a_cutoff():
    declared = {p.destination: p.history_cutoff for p in ALL_WAVE_PAIRINGS if p.history_cutoff}
    assert declared == {
        "mod_accounting.journal_entries": "journal",
        "mod_accounting.journal_lines": "journal_line",
        "mod_accounting.posted_ledger_lines": "ledger_line",
    }
    assert {p.destination for p in WAVE3_PAIRINGS if p.history_cutoff} == set(declared)


def test_parse_cutoff_is_a_calendar_date_only():
    assert hc.parse_cutoff("2025-12-31") == CUTOFF
    for bad in (None, "", "  "):
        with pytest.raises(hc.HistoryCutoffRefused) as exc:
            hc.parse_cutoff(bad)
        assert exc.value.code == hc.REFUSAL_REQUIRED
    for bad in ("2025-12-31T00:00:00", "31/12/2025", "yesterday"):
        with pytest.raises(hc.HistoryCutoffRefused) as exc:
            hc.parse_cutoff(bad)
        assert exc.value.code == hc.REFUSAL_INVALID


# --- the CLI profile ------------------------------------------------------------------------------------------


def execute_args(cutoff):
    return argparse.Namespace(history_cutoff=cutoff)


def journal_specs():
    return (journal_spec(), line_spec(), ledger_spec())


def test_the_cutover_profile_fails_closed_without_the_flag_before_any_database_is_touched():
    with pytest.raises(cli.CliRefusal) as exc:
        cli._history_cutoff(execute_args(None), journal_specs(), None, ())
    assert str(exc.value).startswith(hc.REFUSAL_REQUIRED) and "nothing written" in str(exc.value)
    with pytest.raises(cli.CliRefusal) as exc:
        cli._history_cutoff(execute_args("2025-12-31T00:00"), journal_specs(), None, ())
    assert str(exc.value).startswith(hc.REFUSAL_INVALID)


def test_a_batch_with_no_cutoff_pairing_needs_no_flag_and_a_supplied_flag_is_recorded_as_applying_to_nothing():
    other = build_spec(
        source_table="department",
        destination_table="departments",
        source_columns=("department_id",),
        destination_columns=(col("id"),),
    )
    assert cli._history_cutoff(execute_args(None), (other,), None, ()) == (
        None,
        {"history_cutoff": None, "applies_to": []},
    )
    cutoff, section = cli._history_cutoff(execute_args("2025-12-31"), (other,), None, ())
    assert cutoff == CUTOFF and section["applies_to"] == []


def test_the_parser_accepts_the_flag_on_execute():
    args = cli.build_parser().parse_args(
        ["execute", "--receipt", "r.json", "--manifest", "m.json", "--history-cutoff", "2025-12-31"]
    )
    assert args.history_cutoff == "2025-12-31"


def test_no_destination_document_table_references_the_journal_tables():
    """Pre-cutoff documents keep the legacy journal id as evidence: nothing imported has an FK to a journal row, so
    excluding a journal never excludes or rewrites a document."""

    tables = cli._destination_tables()
    journal_tables = {
        "mod_accounting.journal_entries",
        "mod_accounting.journal_lines",
        "mod_accounting.posted_ledger_lines",
    }
    gl_children = journal_tables | {
        "mod_accounting.journal_line_dimensions",
        "mod_accounting.posted_ledger_dimensions",
    }
    offenders = []
    for name, table in tables.items():
        if name in gl_children:
            continue
        for fk in table.foreign_keys:
            parent = fk.column.table
            if f"{parent.schema}.{parent.name}" in journal_tables:
                offenders.append((name, fk.parent.name))
    assert offenders == []


# --- the WHT gate ---------------------------------------------------------------------------------------------


def test_the_unposted_wht_class_is_registered_as_e04():
    assert IMPORT_CLASS["unposted_wht_line"] == "E04"
