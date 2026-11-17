"""Legacy VOID journals through the accounting owner's governed void path, and the functional
currency mint, on real PostgreSQL (hosted CI only: ERP_TEST_POSTGRES_URL via the shared fixture)."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.idempotency_models import IdempotencyRecord
from sqlalchemy.orm import Session
from test_journals_pg import (  # noqa: F401  (hosted-Postgres fixtures)
    _registries,
    legacy_draft,
    seed,
)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.importer.minting import MintContext, mint_functional_currency_settings
from erp_v2.importer.spec import DestinationColumn, build_spec
from erp_v2.importer.transitions import (
    LEGACY_REQUESTER_PREFIX,
    LEGACY_VOID_REASON,
    LEGACY_VOID_STAMPED_REASON,
    MIGRATION_APPROVER_PREFIX,
    VoidGovernance,
    run_transitions,
)
from erp_v2.journals import policy, queries


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


NOW = datetime(2026, 1, 15, tzinfo=UTC)
ACTOR = str(uuid4())
APPROVAL_REF = "legacy-migration:rev1:0123456789abcdef"
GOVERNANCE = VoidGovernance(migration_actor=ACTOR, approval_reference=APPROVAL_REF, voided_at=NOW)


def _spec(source_table: str):
    return build_spec(
        source_table=source_table,
        destination_table="mod_accounting.journal_entries",
        source_columns=("journal_entry_id", "status", "created_by_user_id"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("status", nullable=False, has_default=False),
        ),
        renames={"id": "journal_entry_id"},
        constants={"status": "DRAFT"},
    )


def _legacy_source(engine, rows) -> str:
    schema = f"legacy_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(
            f"CREATE TABLE {schema}.journal_entry (journal_entry_id uuid, status text, created_by_user_id uuid)"
        )
        for journal_id, creator in rows:
            c.execute(
                sa.text(f"INSERT INTO {schema}.journal_entry VALUES (:j, 'VOID', :c)"),  # noqa: S608
                {"j": journal_id, "c": creator},
            )
    return f"{schema}.journal_entry"


def _frozen_content(engine, journal_id):
    with engine.connect() as c:
        return c.execute(
            sa.text(
                "SELECT (to_jsonb(j) - 'status' - 'updated_at')::text "
                "FROM mod_accounting.journal_entries j WHERE id = :j"
            ),
            {"j": journal_id},
        ).scalar_one(), c.scalar(
            sa.text("SELECT count(*) FROM mod_accounting.journal_lines WHERE journal_id = :j"), {"j": journal_id}
        )


def _legacy_ledger_table(engine, journal_ids) -> tuple[str, str]:
    schema = f"legacy_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"CREATE TABLE {schema}.posted_ledger_line (journal_entry_id uuid)")
        for j in journal_ids:
            c.execute(sa.text(f"INSERT INTO {schema}.posted_ledger_line VALUES (:j)"), {"j": j})  # noqa: S608
    return f"{schema}.posted_ledger_line", "journal_entry_id"


def _run(engine, source_table, governance=GOVERNANCE, ledger_ids=()):
    legacy_ledger = _legacy_ledger_table(engine, ledger_ids)
    with engine.connect() as source, engine.connect() as dest:
        return run_transitions(
            source, dest, (_spec(source_table),), commit=True, void_governance=governance, legacy_ledger=legacy_ledger
        )


def _counts(engine, env):
    with Session(engine) as db:
        events = db.scalar(
            sa.text(
                "SELECT count(*) FROM mod_accounting.period_events WHERE tenant_id = :t AND event_kind = 'JOURNAL_VOID'"
            ),
            {"t": env.tenant_id},
        )
        idem = db.scalar(
            sa.select(sa.func.count())
            .select_from(IdempotencyRecord)
            .where(
                IdempotencyRecord.tenant_id == env.tenant_id, IdempotencyRecord.scope == "accounting.void_draft_journal"
            )
        )
        voided = db.scalar(
            sa.text("SELECT count(*) FROM mod_accounting.journal_entries WHERE tenant_id = :t AND status = 'VOID'"),
            {"t": env.tenant_id},
        )
    return SimpleNamespace(events=events, idempotency=idem, voided=voided)


def test_legacy_voids_land_via_journal_void_events_and_replay_is_a_noop(engine):
    env = seed(engine)
    ordinary, collides, no_creator, posted_evidence = (
        legacy_draft(engine, env, document=name) for name in ("ord", "col", "nocr", "pe")
    )
    with engine.begin() as c:
        c.execute(
            sa.text("UPDATE mod_accounting.journal_entries SET approval_reference = 'legacy-approval' WHERE id = :j"),
            {"j": posted_evidence.id},
        )
    creator = uuid4()
    held_before = None
    with Session(engine) as db:
        held_before = queries.held_journals(db, tenant_id=env.tenant_id, page=1)["total"]
    content_before = {j.id: _frozen_content(engine, j.id) for j in (ordinary, collides)}
    missing = uuid4()  # legacy VOID with no destination row
    source_table = _legacy_source(
        engine,
        [
            (ordinary.id, creator),
            # Same person id as the migration actor: raw ids would collide; prefixes must keep them distinct.
            (collides.id, ACTOR),
            (no_creator.id, None),
            (posted_evidence.id, creator),
            (missing, creator),
        ],
    )

    (outcome,) = _run(engine, source_table)

    assert outcome.transitioned == {"VOID": 2}
    reasons = sorted(e.reason for e in outcome.exceptions)
    assert len(reasons) == 3
    assert any("no created_by_user_id" in r for r in reasons)
    assert any("posting evidence" in r for r in reasons)
    assert any("was not imported" in r for r in reasons)
    counts = _counts(engine, env)
    assert (counts.events, counts.idempotency, counts.voided) == (2, 2, 2)
    with engine.connect() as c:
        events = c.execute(
            sa.text(
                "SELECT actor_ref, approval_reference, evidence->>'approved_by' AS approver, "
                "evidence->>'reason' AS reason, evidence->>'journal_id' AS j, from_status, to_status "
                "FROM mod_accounting.period_events WHERE tenant_id = :t AND event_kind = 'JOURNAL_VOID'"
            ),
            {"t": env.tenant_id},
        ).all()
    by_journal = {e.j: e for e in events}
    assert by_journal[str(ordinary.id)].actor_ref == f"{LEGACY_REQUESTER_PREFIX}{creator}"
    assert by_journal[str(collides.id)].actor_ref == f"{LEGACY_REQUESTER_PREFIX}{ACTOR}"
    for e in events:
        assert e.approver == f"{MIGRATION_APPROVER_PREFIX}{ACTOR}"
        assert e.actor_ref.lower() != e.approver.lower()
        assert (e.reason, e.approval_reference) == (LEGACY_VOID_REASON, APPROVAL_REF)
        assert (e.from_status, e.to_status) == ("DRAFT", "VOID")
    # Content unchanged; the failed rows stay DRAFT; the held queue lost exactly the voided pair.
    assert {j.id: _frozen_content(engine, j.id) for j in (ordinary, collides)} == content_before
    with engine.connect() as c:
        statuses = dict(
            c.execute(
                sa.text("SELECT id, status FROM mod_accounting.journal_entries WHERE tenant_id = :t"),
                {"t": env.tenant_id},
            ).all()
        )
    assert statuses[no_creator.id] == statuses[posted_evidence.id] == "DRAFT"
    with Session(engine) as db:
        assert queries.held_journals(db, tenant_id=env.tenant_id, page=1)["total"] == held_before - 2

    # Replay: nothing moves, nothing is added; the same two exceptions repeat.
    (again,) = _run(engine, source_table)
    assert again.transitioned.get("VOID", 0) == 0
    assert len(again.exceptions) == 3
    assert _counts(engine, env) == counts


def test_no_migration_actor_names_every_void_and_changes_nothing(engine):
    env = seed(engine)
    journal = legacy_draft(engine, env, document="noactor")
    source_table = _legacy_source(engine, [(journal.id, uuid4())])
    (outcome,) = _run(engine, source_table, governance=None)
    assert outcome.transitioned == {}
    assert [e.source_key for e in outcome.exceptions] == [str(journal.id)]
    assert _counts(engine, env).events == 0


def test_functional_currency_mint_lands_the_policy_row_the_b_preflights_read(engine):
    env = seed(engine, currency=None)
    with Session(engine) as db, pytest.raises(Exception) as unset:
        policy.functional_currency(db, env.tenant_id)
    assert "functional_currency_unset" in str(getattr(unset.value, "code", unset.value))
    source_engine = sa.create_engine("sqlite://")
    with source_engine.begin() as c:
        c.execute(sa.text("CREATE TABLE organization (organization_id TEXT, functional_currency_code TEXT)"))
        c.execute(sa.text("INSERT INTO organization VALUES (:o, 'ngn')"), {"o": env.tenant_id.hex})
    ctx = MintContext(tenant_id=env.tenant_id.hex, occurred_at=NOW)
    with source_engine.connect() as source, engine.connect() as dest:
        first = mint_functional_currency_settings(source, dest, ctx)
        dest.commit()
        replay = mint_functional_currency_settings(source, dest, ctx)
    assert (first.created, first.existing, first.blockers) == (1, 0, [])
    assert (replay.created, replay.existing) == (0, 1)
    with Session(engine) as db:
        assert policy.functional_currency(db, env.tenant_id) == "NGN"


def _stamp(engine, journal_id):
    with engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE mod_accounting.journal_entries SET posted_by = 'legacy-user-secret', "
                "posted_at = '2025-03-04T05:06:07+00' WHERE id = :j"
            ),
            {"j": journal_id},
        )


def _ledger_line(engine, journal_id):
    """A legacy posted ledger line for a still-DRAFT journal (the legacy ledger evidence)."""
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO mod_accounting.posted_ledger_lines (id, tenant_id, journal_id, journal_line_id, "
                "fiscal_period_id, account_id, account_code, journal_number, entry_date, posting_date, "
                "currency_code, debit, credit, original_debit, original_credit, exchange_rate, source_owner, "
                "source_document_kind, source_document_id, source_version, source_fingerprint, posted_by, posted_at) "
                "SELECT gen_random_uuid(), j.tenant_id, j.id, l.id, j.fiscal_period_id, l.account_id, 'X', j.number, "
                "j.entry_date, j.posting_date, j.currency_code, l.debit, l.credit, l.debit, l.credit, 1, "
                "'legacy-erp', 'journal', 'x', '1', repeat('b', 64), 'p', now() "
                "FROM mod_accounting.journal_entries j JOIN mod_accounting.journal_lines l ON l.journal_id = j.id "
                "WHERE j.id = :j AND l.debit > 0"
            ),
            {"j": journal_id},
        )


def _without_stamps(text):
    return {k: v for k, v in json.loads(text).items() if k not in ("posted_at", "posted_by")}


def _journal_state(engine, journal_id):
    with engine.connect() as c:
        return c.execute(
            sa.text("SELECT status, posted_at, posted_by FROM mod_accounting.journal_entries WHERE id = :j"),
            {"j": journal_id},
        ).one()


def test_stamped_void_lands_without_stamps_with_new_reason_and_receipt_evidence(engine):
    env = seed(engine)
    stamped, plain, with_ledger, plain_two = (
        legacy_draft(engine, env, document=name) for name in ("st", "pl", "led", "pl2")
    )
    for j in (stamped, with_ledger):
        _stamp(engine, j.id)
    _ledger_line(engine, with_ledger.id)
    content_before = {j.id: _frozen_content(engine, j.id) for j in (stamped, plain)}
    creator = uuid4()
    source_table = _legacy_source(
        engine, [(stamped.id, creator), (plain.id, creator), (with_ledger.id, creator), (plain_two.id, creator)]
    )

    (outcome,) = _run(engine, source_table)

    # Stamped + unstamped VOID land; the VOID with legacy ledger lines stays a named exception, stamps intact.
    assert outcome.transitioned["VOID"] == 3  # stamped, plain, plain_two
    assert [e.source_key for e in outcome.exceptions] == [str(with_ledger.id)]
    assert "posted ledger lines" in outcome.exceptions[0].reason
    ledger_state = _journal_state(engine, with_ledger.id)
    assert ledger_state.status == "DRAFT" and ledger_state.posted_by == "legacy-user-secret"
    assert ledger_state.posted_at is not None
    # Receipt evidence: id and has-stamp flags only, never the stamp values.
    assert outcome.void_stamp_evidence == [
        {"journal_id": str(stamped.id), "had_posted_at": True, "had_posted_by": True}
    ]
    assert "legacy-user-secret" not in str(outcome.void_stamp_evidence)
    after = _journal_state(engine, stamped.id)
    assert (after.status, after.posted_at, after.posted_by) == ("VOID", None, None)
    assert _journal_state(engine, plain.id).status == "VOID"
    with engine.connect() as c:
        reasons = dict(
            c.execute(
                sa.text(
                    "SELECT evidence->>'journal_id', evidence->>'reason' FROM mod_accounting.period_events "
                    "WHERE tenant_id = :t AND event_kind = 'JOURNAL_VOID'"
                ),
                {"t": env.tenant_id},
            ).all()
        )
    assert reasons[str(stamped.id)] == LEGACY_VOID_STAMPED_REASON
    assert reasons[str(plain.id)] == LEGACY_VOID_REASON
    assert LEGACY_VOID_STAMPED_REASON == "voided in legacy ERP after posting; no legacy ledger lines"
    # Content otherwise unchanged (status/updated_at excluded; posted stamps are the only change).
    for j in (stamped, plain):
        before_json, before_lines = content_before[j.id]
        after_json, after_lines = _frozen_content(engine, j.id)
        assert after_lines == before_lines
        assert _without_stamps(after_json) == _without_stamps(before_json)
    counts = _counts(engine, env)

    # Replay is a no-op: nothing moves, nothing is recorded again, same single exception.
    (again,) = _run(engine, source_table)
    assert again.transitioned.get("VOID", 0) == 0
    assert again.void_stamp_evidence == []
    assert [e.source_key for e in again.exceptions] == [str(with_ledger.id)]
    assert _counts(engine, env) == counts


def test_void_with_reversal_link_stays_a_named_exception_and_is_not_nulled(engine):
    env = seed(engine)
    journal, target = legacy_draft(engine, env, document="rl"), legacy_draft(engine, env, document="rt")
    _stamp(engine, journal.id)
    with engine.begin() as c:
        c.execute(
            sa.text("UPDATE mod_accounting.journal_entries SET reversal_journal_id = :t WHERE id = :j"),
            {"t": target.id, "j": journal.id},
        )
    source_table = _legacy_source(engine, [(journal.id, uuid4())])
    (outcome,) = _run(engine, source_table)
    assert outcome.transitioned.get("VOID", 0) == 0 and outcome.void_stamp_evidence == []
    assert "reversal link" in outcome.exceptions[0].reason
    state = _journal_state(engine, journal.id)
    assert state.status == "DRAFT" and state.posted_by == "legacy-user-secret"


def test_stamped_void_with_legacy_ledger_line_absent_in_destination_stays_a_named_exception(engine):
    env = seed(engine)
    journal, clean = legacy_draft(engine, env, document="ll"), legacy_draft(engine, env, document="lc")
    _stamp(engine, journal.id)
    _stamp(engine, clean.id)
    source_table = _legacy_source(engine, [(journal.id, uuid4()), (clean.id, uuid4())])
    # The legacy ledger line was rejected at import: the destination has none for this journal.
    (outcome,) = _run(engine, source_table, ledger_ids=[journal.id])
    assert [e.source_key for e in outcome.exceptions] == [str(journal.id)]
    assert "legacy ledger lines" in outcome.exceptions[0].reason
    state = _journal_state(engine, journal.id)
    assert state.status == "DRAFT" and state.posted_by == "legacy-user-secret" and state.posted_at is not None
    assert [e["journal_id"] for e in outcome.void_stamp_evidence] == [str(clean.id)]
    assert _journal_state(engine, clean.id).status == "VOID"


def test_stamped_void_without_a_legacy_ledger_source_is_refused_not_nulled(engine):
    env = seed(engine)
    journal = legacy_draft(engine, env, document="nl")
    _stamp(engine, journal.id)
    source_table = _legacy_source(engine, [(journal.id, uuid4())])
    with engine.connect() as source, engine.connect() as dest:
        (outcome,) = run_transitions(source, dest, (_spec(source_table),), commit=True, void_governance=GOVERNANCE)
    assert "ledger source is unavailable" in outcome.exceptions[0].reason
    assert _journal_state(engine, journal.id).posted_by == "legacy-user-secret"
