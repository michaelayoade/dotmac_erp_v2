"""Legacy VOID journals through the accounting owner's governed void path, and the functional
currency mint, on real PostgreSQL (hosted CI only: ERP_TEST_POSTGRES_URL via the shared fixture)."""

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


def _run(engine, source_table, governance=GOVERNANCE):
    with engine.connect() as source, engine.connect() as dest:
        return run_transitions(source, dest, (_spec(source_table),), commit=True, void_governance=governance)


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
            sa.text("UPDATE mod_accounting.journal_entries SET posted_by = 'legacy-system' WHERE id = :j"),
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
