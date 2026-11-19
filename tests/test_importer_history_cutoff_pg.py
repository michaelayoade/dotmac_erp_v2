# ruff: noqa: E501  (long legacy-schema SQL fixtures)
"""B-13 on real PostgreSQL: the importer's history cutoff against the real journal pairings and the real B-9 opening.

v2 must hold NO legacy-owner ledger line on or before the cutoff, and the opening journal plus the post-cutoff
journals must reproduce the legacy trial balance at cutoff + 1 day (P&L rolled into 3100 in the opening).

Hosted CI only (ERP_TEST_POSTGRES_URL), like test_migration_ledger_pg.
"""

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting.models import Account, JournalEntry, PostedLedgerLine
from sqlalchemy.orm import Session
from test_journals_pg import _registries  # noqa: F401  (hosted-Postgres fixture)
from test_migration_ledger_pg import SHA, apply_inputs, world
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.importer import cli, history_cutoff
from erp_v2.importer.enrolment import EnrolmentMaps
from erp_v2.importer.pipeline import order_specs, run_import
from erp_v2.importer.transitions import run_transitions
from erp_v2.importer.waves36 import WAVE3_PAIRINGS, build_specs
from erp_v2.migration_ledger import legacy_ledger, opening

CUTOFF = date(2025, 12, 31)
NEXT_DAY = CUTOFF + timedelta(days=1)
OWNER = "legacy-erp"
NOW = datetime(2026, 1, 15, tzinfo=UTC)
TENANT_JOURNAL_PAIRINGS = (
    "mod_accounting.journal_entries",
    "mod_accounting.journal_lines",
    "mod_accounting.posted_ledger_lines",
)


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


LEGACY_DDL = (
    "CREATE SCHEMA gl",
    "CREATE TABLE gl.journal_entry (journal_entry_id uuid PRIMARY KEY, organization_id uuid, fiscal_period_id uuid, "
    "journal_number text, journal_type text, status text, entry_date date, posting_date date, description text, "
    "currency_code text, exchange_rate numeric, total_debit numeric, total_credit numeric, total_debit_functional numeric, "
    "total_credit_functional numeric, source_document_type text, source_document_id text, posted_by_user_id text, "
    "posted_at timestamptz, reversed_journal_id uuid, reversal_journal_id uuid, created_by_user_id uuid, reference text)",
    "CREATE TABLE gl.journal_entry_line (line_id uuid PRIMARY KEY, journal_entry_id uuid, organization_id uuid, "
    "line_number int, account_id uuid, description text, debit_amount numeric, credit_amount numeric, "
    "debit_amount_functional numeric, credit_amount_functional numeric)",
    "CREATE TABLE gl.posted_ledger_line (ledger_line_id uuid PRIMARY KEY, organization_id uuid, journal_entry_id uuid, "
    "journal_line_id uuid, fiscal_period_id uuid, account_id uuid, account_code text, entry_date date, posting_date date, "
    "description text, original_currency_code text, debit_amount numeric, credit_amount numeric, "
    "original_debit_amount numeric, original_credit_amount numeric, exchange_rate numeric, source_document_type text, "
    "source_document_id text, posted_by_user_id text, posted_at timestamptz)",
)


@pytest.fixture
def legacy(engine):
    with engine.begin() as c:
        for statement in LEGACY_DDL:
            c.exec_driver_sql(statement)
    yield
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA gl CASCADE")


def post_legacy(engine, env, jid, day, lines, *, number, status="POSTED", ledger=True):
    """One legacy journal: lines (account code, debit, credit); posted journals also carry posted ledger lines."""
    total = sum(Decimal(d) for _, d, _ in lines)
    period = env.periods[1]
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO gl.journal_entry VALUES (:j, :o, :p, :n, 'STANDARD', :s, :d, :d, :n, 'NGN', 1, :t, :t, :t, :t, "
                "'JOURNAL', :j, 'legacy-user', :at, NULL, NULL, NULL, NULL)"
            ),
            {"j": jid, "o": env.tenant_id, "p": period, "n": number, "s": status, "d": day, "t": total, "at": NOW},
        )
        for n, (code, debit, credit) in enumerate(lines, 1):
            line_id = uuid4()
            amounts = {"d": Decimal(debit), "c": Decimal(credit)}
            c.execute(
                sa.text("INSERT INTO gl.journal_entry_line VALUES (:l, :j, :o, :n, :a, :ds, :d, :c, :d, :c)"),
                {
                    "l": line_id,
                    "j": jid,
                    "o": env.tenant_id,
                    "n": n,
                    "a": env.accounts[code],
                    "ds": f"{number} line {n}",
                    **amounts,
                },
            )
            if ledger:
                c.execute(
                    sa.text(
                        "INSERT INTO gl.posted_ledger_line VALUES (:id, :o, :j, :l, :p, :a, :code, :day, :day, :ds, 'NGN', "
                        ":d, :c, :d, :c, 1, 'JOURNAL', :j, 'legacy-user', :at)"
                    ),
                    {
                        "id": uuid4(),
                        "o": env.tenant_id,
                        "j": jid,
                        "l": line_id,
                        "p": period,
                        "a": env.accounts[code],
                        "code": code,
                        "day": day,
                        "ds": f"{number} line {n}",
                        "at": NOW,
                        **amounts,
                    },
                )


def seed_legacy(engine, env):
    """Closed years (on or before the cutoff, one ON it) and post-cutoff activity; P&L never closed in legacy."""
    ids = {}
    for key, day, lines, kwargs in (
        ("j0", date(2025, 1, 1), [("1000", "50", "0"), ("3100", "0", "50")], {}),
        ("j1", date(2025, 3, 1), [("1000", "600", "0"), ("4000", "0", "600")], {}),
        ("j2", date(2025, 6, 1), [("5000", "150", "0"), ("1000", "0", "150")], {}),
        ("j3", date(2025, 12, 30), [("1410", "50", "0"), ("2040", "0", "50")], {}),
        ("j4", CUTOFF, [("1400", "300", "0"), ("2000", "0", "300")], {}),  # ON the cutoff
        ("jd_old", date(2025, 9, 9), [("1000", "7", "0"), ("2000", "0", "7")], {"status": "DRAFT", "ledger": False}),
        ("j5", NEXT_DAY, [("1000", "80", "0"), ("4000", "0", "80")], {}),
        ("j6", NEXT_DAY, [("2000", "100", "0"), ("1000", "0", "100")], {}),
        ("j7", date(2026, 1, 15), [("1000", "9", "0"), ("4000", "0", "9")], {}),
        ("jd_new", date(2026, 1, 20), [("1000", "3", "0"), ("2000", "0", "3")], {"status": "DRAFT", "ledger": False}),
    ):
        ids[key] = uuid4()
        post_legacy(engine, env, ids[key], day, lines, number=f"JE-{key}", **kwargs)
    return ids


def journal_specs(engine):
    """The REAL waves36 journal pairings, resolved to the legacy ``gl`` schema exactly as the CLI does."""
    inspector = sa.inspect(engine)
    pairings = tuple(
        dataclasses.replace(p, source=f"gl.{p.source.rpartition('.')[2]}")
        for p in WAVE3_PAIRINGS
        if p.destination in TENANT_JOURNAL_PAIRINGS
    )
    columns = {
        p.source: tuple(c["name"] for c in inspector.get_columns(p.source.partition(".")[2], schema="gl"))
        for p in pairings
    }
    specs, generated = build_specs(pairings, columns, cli._destination_tables())
    assert not generated and all(s.executable for s in specs), [
        (s.destination_table, s.missing_required) for s in specs
    ]
    return order_specs(specs, cli._destination_tables())


def run_importer(engine, env, specs, cutoff):
    constants = {"tenant_id": env.tenant_id, "source_owner": OWNER, "source_version": "b13-test"}
    with engine.connect() as source, engine.connect() as dest:
        source.exec_driver_sql("SET search_path TO gl, public")
        enrolment = EnrolmentMaps(source, dest)
        receipt = run_import(
            source,
            dest,
            specs,
            enrolment=enrolment,
            run_constants=constants,
            commit_per_spec=True,
            history_cutoff=cutoff,
        )
        dest.commit()
        transitions = run_transitions(source, dest, specs, commit=True, history_cutoff=cutoff)
    return receipt, transitions


def v2_net_by_code(engine, env, *, through: date, owner: str | None = None, after: date | None = None):
    with Session(engine) as db:
        query = (
            sa.select(Account.code, sa.func.coalesce(sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0))
            .join(
                Account, (Account.tenant_id == PostedLedgerLine.tenant_id) & (Account.id == PostedLedgerLine.account_id)
            )
            .join(
                JournalEntry,
                (JournalEntry.tenant_id == PostedLedgerLine.tenant_id)
                & (JournalEntry.id == PostedLedgerLine.journal_id),
            )
            .where(PostedLedgerLine.tenant_id == env.tenant_id, PostedLedgerLine.posting_date <= through)
            .group_by(Account.code)
        )
        if owner is not None:
            query = query.where(JournalEntry.source_owner == owner)
        if after is not None:
            query = query.where(PostedLedgerLine.posting_date > after)
        return {code: Decimal(net) for code, net in db.execute(query) if Decimal(net) != 0}


def post_opening_from_legacy(engine, env):
    """The B-9 opening for the cutoff: the legacy cumulative TB (P&L rolled into 3100), control accounts tied to
    their subledgers (equal here, so no IAS 8)."""
    with engine.connect() as legacy_conn, Session(engine) as db:
        net, _universe = legacy_ledger.recompute(legacy_conn, db, env.tenant_id, opening_as_of=CUTOFF)
    tb_net = {code: value for (code, period), value in net.items() if period == "OPENING"}
    rows = [
        {"account_code": code, "debit": str(v) if v > 0 else "0", "credit": str(-v) if v < 0 else "0"}
        for code, v in sorted(tb_net.items())
    ]
    as_of, parsed = opening.parse_tb({"version": 1, "as_of": CUTOFF.isoformat(), "rows": rows}, SHA)
    sub = {code: tb_net.get(code, Decimal(0)) for code in ("2000", "2040", "1410", "1400")}
    inputs = opening.OpeningInputs(as_of, parsed, "c" * 64, sub, "d" * 64, (), "e" * 64)
    with Session(engine) as db:
        out = opening.apply_opening(db, env.tenant_id, inputs, apply_inputs(env))
        db.commit()
    return out, tb_net


def legacy_tb_at(engine, day: date):
    """Legacy TB at ``day`` as v2 must show it: balance-sheet cumulative, P&L = activity after the cutoff only,
    and the P&L up to the cutoff rolled into 3100."""
    with engine.connect() as c:
        rows = c.execute(
            sa.text(
                "SELECT account_code, posting_date, debit_amount - credit_amount FROM gl.posted_ledger_line WHERE posting_date <= :d"
            ),
            {"d": day},
        ).all()
    out: dict[str, Decimal] = {}
    pnl = {"4000", "5000", "6900"}
    for code, posted, net in rows:
        target = code
        if code in pnl and posted <= CUTOFF:
            target = "3100"
        out[target] = out.get(target, Decimal(0)) + Decimal(net)
    return {k: v for k, v in out.items() if v != 0}


def test_v2_has_no_legacy_owner_lines_on_or_before_the_cutoff_and_the_opening_plus_post_cutoff_reproduce_the_legacy_tb(
    engine, legacy
):
    env = world(engine)
    ids = seed_legacy(engine, env)
    specs = journal_specs(engine)

    receipt, transitions = run_importer(engine, env, specs, CUTOFF)
    assert receipt.complete, [e for e in receipt.exceptions][:5]
    assert all(not outcome.exceptions for outcome in transitions)

    # 1. no legacy-owner journal or ledger line dated on or before the cutoff reached v2
    assert v2_net_by_code(engine, env, through=CUTOFF, owner=OWNER) == {}
    with Session(engine) as db:
        landed = {
            str(j)
            for j in db.scalars(
                sa.select(JournalEntry.id).where(
                    JournalEntry.tenant_id == env.tenant_id, JournalEntry.source_owner == OWNER
                )
            )
        }
        pre_ledger = db.scalar(
            sa.select(sa.func.count())
            .select_from(PostedLedgerLine)
            .where(PostedLedgerLine.tenant_id == env.tenant_id, PostedLedgerLine.posting_date <= CUTOFF)
        )
        pre_lines = db.scalar(
            sa.text(
                "SELECT count(*) FROM mod_accounting.journal_lines l JOIN mod_accounting.journal_entries j "
                "ON j.tenant_id = l.tenant_id AND j.id = l.journal_id WHERE l.tenant_id = :t AND j.source_owner = :o AND j.posting_date <= :c"
            ),
            {"t": env.tenant_id, "o": OWNER, "c": CUTOFF},
        )
    assert pre_ledger == 0 and pre_lines == 0
    # exactly the post-cutoff legacy journals landed, including the held post-cutoff draft; no pre-cutoff draft did
    assert landed == {str(ids[k]) for k in ("j5", "j6", "j7", "jd_new")}

    # 2. the receipt census: the excluded pre-cutoff journals are counted with their legacy ids as evidence
    census = {r.destination_table: r for r in receipt.reports}
    journals = census["mod_accounting.journal_entries"]
    assert (
        journals.history_cutoff == "2025-12-31"
        and journals.history_cutoff_source == 10
        and journals.history_cutoff_excluded == 6
    )
    assert journals.history_cutoff_excluded_sha256 == history_cutoff.keys_sha256(
        sorted(str(ids[k]) for k in ("j0", "j1", "j2", "j3", "j4", "jd_old"))
    )
    ledger = census["mod_accounting.posted_ledger_lines"]
    assert (ledger.history_cutoff_source, ledger.history_cutoff_excluded, ledger.written) == (
        16,
        10,
        6,
    )  # 5 closed journals x 2 lines
    lines = census["mod_accounting.journal_lines"]
    assert lines.history_cutoff_excluded == 12 and lines.written == 8  # the closed draft's two lines are excluded too
    assert journals.read == journals.written == 4

    # 3. the B-9 opening carries the closed years, and opening + post-cutoff journals = the legacy TB at cutoff + 1
    out, _tb = post_opening_from_legacy(engine, env)
    assert out["opening"] == 1
    assert v2_net_by_code(engine, env, through=NEXT_DAY) == legacy_tb_at(engine, NEXT_DAY)
    assert sum(v2_net_by_code(engine, env, through=NEXT_DAY).values()) == 0
    # and the same for the whole ledger up to the last post-cutoff day: no line counted twice
    assert v2_net_by_code(engine, env, through=date(2026, 1, 31)) == legacy_tb_at(engine, date(2026, 1, 31))


def test_without_the_cutoff_the_same_import_double_counts_the_closed_years_so_the_flag_is_not_optional(engine, legacy):
    env = world(engine)
    seed_legacy(engine, env)
    specs = journal_specs(engine)
    with engine.connect() as source, engine.connect() as dest, pytest.raises(history_cutoff.HistoryCutoffRefused):
        source.exec_driver_sql("SET search_path TO gl, public")
        run_import(
            source,
            dest,
            specs,
            enrolment=EnrolmentMaps(source, dest),
            run_constants={"tenant_id": env.tenant_id, "source_owner": OWNER, "source_version": "x"},
        )
    assert v2_net_by_code(engine, env, through=date(2030, 1, 1)) == {}  # refused before any write


def test_a_journal_straddling_the_cutoff_is_refused_before_any_write(engine, legacy):
    env = world(engine)
    jid = uuid4()
    post_legacy(engine, env, jid, NEXT_DAY, [("1000", "10", "0"), ("2000", "0", "10")], number="JE-straddle")
    with engine.begin() as c:  # one ledger line of the journal moves to the closed year
        c.execute(
            sa.text(
                "UPDATE gl.posted_ledger_line SET posting_date = :d WHERE ctid = (SELECT ctid FROM gl.posted_ledger_line WHERE journal_entry_id = :j LIMIT 1)"
            ),
            {"d": CUTOFF, "j": jid},
        )
    with engine.connect() as source:
        source.exec_driver_sql("SET search_path TO gl, public")
        assert history_cutoff.straddling_journals(source, CUTOFF) == 1
    specs = journal_specs(engine)
    with pytest.raises(cli.CliRefusal) as exc:
        cli._history_cutoff(type("A", (), {"history_cutoff": "2025-12-31"})(), specs, engine, ["gl"])
    assert str(exc.value).startswith(history_cutoff.REFUSAL_STRADDLE) and "nothing written" in str(exc.value)


def test_p5_a_sub_kobo_unbalanced_journal_copies_its_ledger_lines_and_stays_draft_with_a_named_transition_exception(
    engine, legacy
):
    """P-5 finding: the ledger lines of a 0.000001-unbalanced legacy journal DO copy (NUMERIC(20,6) keeps the amounts,
    only the DRAFT -> POSTED trigger checks balance), so the plan's H-GL fallback (for a ledger that does NOT copy)
    does not apply; the journal itself stays DRAFT and is named."""
    env = world(engine)
    jid = uuid4()
    post_legacy(engine, env, jid, NEXT_DAY, [("1000", "10.000001", "0"), ("2000", "0", "10")], number="JE-subkobo")
    specs = journal_specs(engine)
    receipt, transitions = run_importer(engine, env, specs, CUTOFF)
    ledger = {r.destination_table: r for r in receipt.reports}["mod_accounting.posted_ledger_lines"]
    assert ledger.written == 2 and ledger.read == 2  # both ledger lines copied at their legacy amounts
    assert v2_net_by_code(engine, env, through=NEXT_DAY, owner=OWNER) == {
        "1000": Decimal("10.000001"),
        "2000": Decimal("-10"),
    }
    with Session(engine) as db:
        assert db.scalar(sa.select(JournalEntry.status).where(JournalEntry.id == jid)) == "DRAFT"
    journal_outcome = next(t for t in transitions if t.destination_table == "mod_accounting.journal_entries")
    assert [e.source_key for e in journal_outcome.exceptions] == [str(jid)]
