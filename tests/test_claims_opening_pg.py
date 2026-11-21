# ruff: noqa: E501
"""S8 claims opening on real PostgreSQL (hosted CI only): the real accounting and expenses a2 migrations, the real owner
command ``admit_historical_claim_liability``, the real posting bridge. The legacy snapshot is a read-only SQLite file
with the legacy column names the verifier reads (the settled-admission test pattern)."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting.contracts import AccountClass, AccountKind, JournalKind, JournalStatus, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, JournalLine, PostedLedgerLine
from dotmac_expenses import ClaimStatus, ExpensesError, claims_payable_total
from dotmac_expenses.models import ClaimLiability, ExpenseClaim, HistoricalClaimReceipt
from dotmac_kernel.models import Party, PartyType
from sqlalchemy.orm import Session
from test_journals_pg import NOW, _registries, seed  # noqa: F401  (hosted-Postgres fixtures)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.claims_opening import cli, core, extract, reclass
from erp_v2.claims_opening.reasons import ClaimsRefusal
from erp_v2.claims_opening.verifier import ClaimOpeningVerifier
from erp_v2.holdings import attribution as attr
from erp_v2.holdings import positions
from erp_v2.importer.payables_history import READ_FIELDS
from erp_v2.importer.snapshot import SnapshotManifest
from erp_v2.migration_ledger import ledger_check, register

D = Decimal
SNAP = "d" * 64
REVISION = "e" * 40
OP = "operator-a"
WINDOW = "6f1d0a52-0c6e-4a58-9d0a-3a7c0f0e1b11"
CAPTURE = date(2026, 1, 12)
CUTOFF = date(2025, 12, 31)
POSTED_AT = datetime(2026, 1, 10, tzinfo=UTC)
BOOLEANS = {"is_reversal"}
SOURCE_TABLES = {
    "expense_claim": extract.CLAIM_FIELDS,
    "journal_entry": READ_FIELDS["journal_entry"],
    "posted_ledger_line": READ_FIELDS["posted_ledger_line"],
}


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


def norm(column, value):
    if column.endswith("_date") and isinstance(value, str):
        return date.fromisoformat(value)
    return value


def world(engine) -> SimpleNamespace:
    env = seed(engine, currency="NGN")
    with Session(engine) as db:
        cat = db.scalar(
            sa.select(AccountCategory).where(
                AccountCategory.tenant_id == env.tenant_id, AccountCategory.account_class == AccountClass.LIABILITY
            )
        )
        for code in ("2045", "2030", "2110"):
            row = Account(
                tenant_id=env.tenant_id, category_id=cat.id, code=code, name=f"A{code}",
                kind=AccountKind.POSTING, normal_balance=NormalBalance.CREDIT,
            )  # fmt: skip
            db.add(row)
            db.flush()
            env.accounts[code] = row.id
        db.commit()
    return env


def post_opening(engine, env, day=CUTOFF):
    """The B-9 opening journal (owner ``erp.opening``, POSTED, dated the history cutoff)."""
    insert_journal(engine, env, uuid4(), owner="erp.opening", day=day)


def insert_journal(engine, env, jid, *, owner="legacy-erp", day=date(2026, 1, 10)):
    with Session(engine) as db:
        db.add(
            JournalEntry(
                id=jid,
                tenant_id=env.tenant_id,
                fiscal_period_id=env.periods[1],
                number=f"N-{jid.hex[:12]}",
                kind=JournalKind.STANDARD,
                status=JournalStatus.DRAFT,
                entry_date=day,
                posting_date=day,
                description="Legacy journal",
                currency_code="NGN",
                exchange_rate=D(1),
                total_debit=D(1),
                total_credit=D(1),
                total_debit_functional=D(1),
                total_credit_functional=D(1),
                source_owner=owner,
                source_document_kind="EXPENSE_CLAIM",
                source_document_id=f"legacy:gl.journal_entry:{jid}",
                source_version=REVISION,
                source_fingerprint="a" * 64,
                request_fingerprint="b" * 64,
                approval_reference=f"legacy:gl.journal_entry:{jid}@{day}",
                posted_by="legacy-poster",
                posted_at=POSTED_AT,
            )
        )
        db.flush()
        for n, (code, debit, credit) in enumerate((("1000", D(1), D(0)), ("4000", D(0), D(1))), 1):
            db.add(
                JournalLine(
                    tenant_id=env.tenant_id,
                    journal_id=jid,
                    line_number=n,
                    account_id=env.accounts[code],
                    description=None,
                    debit=debit,
                    credit=credit,
                    debit_functional=debit,
                    credit_functional=credit,
                )
            )
        db.flush()
        for line in db.scalars(sa.select(JournalLine).where(JournalLine.journal_id == jid)):
            code = "1000" if line.debit else "4000"
            db.add(
                PostedLedgerLine(
                    tenant_id=env.tenant_id,
                    journal_id=jid,
                    journal_line_id=line.id,
                    fiscal_period_id=env.periods[1],
                    account_id=line.account_id,
                    account_code=code,
                    journal_number=f"N-{jid.hex[:12]}",
                    entry_date=day,
                    posting_date=day,
                    description=None,
                    currency_code="NGN",
                    debit=line.debit,
                    credit=line.credit,
                    original_debit=line.debit,
                    original_credit=line.credit,
                    exchange_rate=D(1),
                    source_owner=owner,
                    source_document_kind="EXPENSE_CLAIM",
                    source_document_id=f"legacy:gl.journal_entry:{jid}",
                    source_version=REVISION,
                    source_fingerprint="c" * 64,
                    posted_by="legacy-poster",
                    posted_at=POSTED_AT,
                )
            )
        db.flush()
        db.execute(sa.update(JournalEntry).where(JournalEntry.id == jid).values(status=JournalStatus.POSTED))
        db.commit()


class Scene:
    """Legacy snapshot rows plus the imported destination rows for a set of claims of one tenant."""

    def __init__(self, engine, env, tmp_path, *, opening=True):
        self.engine, self.env, self.path = engine, env, tmp_path / "source.db"
        self.rows = {name: [] for name in SOURCE_TABLES}
        self.employees: dict[str, UUID] = {}  # legacy employee id -> party id
        self.claims: list[str] = []
        with engine.connect() as dest:
            self.destination_name = dest.scalar(sa.text("SELECT current_database()"))
        if opening:
            post_opening(engine, env)

    # -- legacy rows
    def employee(self) -> str:
        eid, party = str(uuid4()), uuid4()
        with Session(self.engine) as db:
            db.add(
                Party(id=party, tenant_id=self.env.tenant_id, party_type=PartyType.person,
                      display_name=f"Zed Claimant {party.hex[:8]}", email=f"{party.hex[:10]}@claims.test")
            )  # fmt: skip
            db.commit()
        self.employees[eid] = party
        return eid

    def journal(self, lines, *, doc_type="EXPENSE_CLAIM", doc_id=None, day="2026-01-10", dest=True, ledger_day=None,
                undated=False, tenant=None):  # fmt: skip
        """lines: (account code, debit, credit). Returns the legacy journal id (also the destination id)."""
        jid, tenant = uuid4(), tenant or self.env.tenant_id
        total = sum(D(d) for _c, d, _cr in lines)
        row = dict.fromkeys(READ_FIELDS["journal_entry"])
        row.update(
            journal_entry_id=str(jid), organization_id=str(tenant), journal_number=f"J-{jid.hex[:8]}",
            journal_type="STANDARD", status="POSTED", source_module="EXP", source_document_type=doc_type,
            source_document_id=None if doc_id is None else str(doc_id), is_reversal=False, currency_code="NGN",
            exchange_rate="1", entry_date=None if undated else day, posting_date=None if undated else day,
            total_debit=str(total), total_credit=str(total), total_debit_functional=str(total),
            total_credit_functional=str(total), posted_at=POSTED_AT.isoformat(),
        )  # fmt: skip
        self.rows["journal_entry"].append(row)
        for code, debit, credit in lines:
            line = dict.fromkeys(READ_FIELDS["posted_ledger_line"])
            line.update(
                ledger_line_id=str(uuid4()), organization_id=str(tenant), journal_entry_id=str(jid),
                account_code=code, posting_date=ledger_day or day, entry_date=ledger_day or day,
                debit_amount=str(debit), credit_amount=str(credit), source_module="EXP",
                source_document_type=doc_type, source_document_id=None if doc_id is None else str(doc_id),
            )  # fmt: skip
            self.rows["posted_ledger_line"].append(line)
        if dest and tenant == self.env.tenant_id:
            insert_journal(self.engine, self.env, jid)
        return jid

    def claim(self, *, net="100.00", carried="2110", reclass_to=None, posted=True, status="APPROVED", paid="0",
              dest=True, dest_journal=True, net_null=False, balance=None, currency="NGN", advance="0", employee="new",
              journal_day="2026-01-10", ledger_day=None, undated=False, credit_lines=None, reimb=None,
              tenant=None, claim_id=None):  # fmt: skip
        """One legacy claim. ``carried``: the family account its claim journal credits; ``reclass_to``: a claim-linked
        RECLASS line pair moving the credit from ``carried`` to that account."""
        cid = claim_id or uuid4()
        eid = self.employee() if employee == "new" else employee
        tenant = tenant or self.env.tenant_id
        jid = None
        if posted:
            lines = credit_lines or [("6000", net, "0"), (carried, "0", net)]
            jid = self.journal(lines, doc_id=cid, day=journal_day, dest=dest_journal, ledger_day=ledger_day,
                               undated=undated, tenant=tenant)  # fmt: skip
        if reclass_to:
            self.journal([(carried, net, "0"), (reclass_to, "0", net)], doc_type="RECLASS", doc_id=cid)
        reimb_id = None
        if reimb is not None:
            reimb_id = self.journal(reimb, doc_type="EXPENSE_REIMBURSEMENT", doc_id=cid)
        net_value = None if net_null else D(net)
        owed = None if net_value is None else net_value - D(paid)
        row = dict(
            claim_id=str(cid), organization_id=str(tenant), employee_id=eid, status=status, currency_code=currency,
            net_payable_amount=None if net_null else net, amount_paid=paid,
            balance_due=balance if balance is not None else (None if owed is None else str(owed)),
            advance_adjusted=advance, journal_entry_id=None if jid is None else str(jid),
            reimbursement_journal_id=None if reimb_id is None else str(reimb_id),
        )  # fmt: skip
        self.rows["expense_claim"].append(row)
        if dest and eid is not None and tenant == self.env.tenant_id:
            with Session(self.engine) as db:
                db.add(
                    ExpenseClaim(
                        id=cid, tenant_id=tenant, reference=f"EC-{cid.hex[:10]}", claimant_party_id=self.employees[eid],
                        purpose="legacy", claim_date=date(2026, 1, 5), currency_code="NGN",
                        total_claimed_amount=D(net), status=ClaimStatus.APPROVED,
                    )
                )  # fmt: skip
                db.commit()
        self.claims.append(str(cid))
        return cid

    def build(self):
        self.path.unlink(missing_ok=True)
        source = sa.create_engine(f"sqlite:///{self.path}")
        with source.begin() as conn:
            for name, fields in SOURCE_TABLES.items():
                table = sa.Table(
                    name,
                    sa.MetaData(),
                    *(
                        sa.Column(f, sa.Boolean if f in BOOLEANS else sa.Date if f.endswith("_date") else sa.Text)
                        for f in fields
                    ),
                )
                table.create(conn)
                if self.rows[name]:
                    conn.execute(table.insert(), [{k: norm(k, v) for k, v in row.items()} for row in self.rows[name]])
        source.dispose()

    # -- runs
    def manifest(self):
        return SnapshotManifest(SNAP, "source.db", self.destination_name, REVISION, (str(self.env.tenant_id),))

    def run(self, *, apply, digest=None, cutoff=CUTOFF, verifier_cls=ClaimOpeningVerifier, capture=CAPTURE):
        source = sa.create_engine(f"sqlite:///{self.path}")
        try:
            with source.connect() as src:
                src.exec_driver_sql("PRAGMA query_only=1")
                with Session(self.engine) as db, db.begin():
                    if not apply:
                        db.execute(sa.text("SET TRANSACTION READ ONLY"))
                    verifier = verifier_cls(src, self.manifest(), cutoff, OP)
                    kw = dict(window_id=WINDOW, capture_date=capture, bindings=dict(tenant=str(self.env.tenant_id)))
                    if not apply:
                        return core.build_plan(db, src, verifier, self.env.tenant_id, admitted_at=NOW, **kw)
                    return core.apply_plan(db, src, verifier, self.env.tenant_id, reviewed_digest=digest, now=NOW, **kw)
        finally:
            source.dispose()

    def plan(self, **kw):
        return self.run(apply=False, **kw)

    def apply(self, plan=None, **kw):
        plan = plan or self.plan(**kw)
        return self.run(apply=True, digest=plan["digest"], **kw)


def counts(engine, tenant) -> dict:
    with Session(engine) as db:
        out = {
            m.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == tenant))
            for m in (ClaimLiability, HistoricalClaimReceipt)
        }
        out["reclass_journals"] = len(
            list(
                db.scalars(
                    sa.select(JournalEntry.id).where(
                        JournalEntry.tenant_id == tenant, JournalEntry.source_document_kind == "claim_family_reclass"
                    )
                )
            )
        )
        return out


def gl(engine, env, code) -> Decimal:
    with Session(engine) as db:
        return positions.gl_net(db, env.tenant_id, env.accounts[code])


def census_scene(engine, tmp_path):
    """The reconciliation fixture: 62 posted open claims over 32 employees (12 moved 2040 -> 2030 by claim-linked
    RECLASS lines: 11 x 5,000.00 + 1 x 15,305.10 = 70,305.10; 50 x 9,178.00 on 2110), 3 unposted, 1 no-net-payable with
    a posted journal, 1 PAID with an artefact balance, 1 APPROVAL_WITHDRAWN."""
    env = world(engine)
    scene = Scene(engine, env, tmp_path)
    employees = [scene.employee() for _ in range(32)]
    n = 0
    for amount in ["5000.00"] * 11 + ["15305.10"]:
        scene.claim(net=amount, carried="2040", reclass_to="2030", employee=employees[n % 32])
        n += 1
    for _ in range(50):
        scene.claim(net="9178.00", carried="2110", employee=employees[n % 32])
        n += 1
    for _ in range(3):
        scene.claim(net="100.00", posted=False)
    scene.claim(net_null=True, posted=True, dest=False)
    scene.claim(net="100.00", status="PAID", balance="100.00", posted=True)
    scene.claim(net="100.00", status="APPROVAL_WITHDRAWN", posted=False, balance="100.00")
    scene.build()
    return env, scene


# --- p1 -----------------------------------------------------------------------------------------------------------


def test_p1_the_census_reconciles_62_529205_10_32_with_the_carried_split(engine, tmp_path):
    env, scene = census_scene(engine, tmp_path)
    plan = scene.plan()
    assert plan["buckets"]["A"] == dict(count=62, sum="529205.10", employees=32, partial=0)
    assert plan["carried_split"]["2030"] == dict(count=12, sum="70305.10")
    assert plan["carried_split"]["2110"] == dict(count=50, sum="458900.00")
    assert plan["carried_split"]["2040"] == dict(count=0, sum="0.00")
    assert plan["buckets"]["U"]["count"] == 3
    assert plan["refusals_by_reason"] == {"claim_net_payable_missing": 1}
    assert plan["buckets"]["N"] == dict(count=1, refused_with_posted_journal=1)
    assert plan["buckets"]["S"] == dict(count=1, paid_with_balance_artefact=1)
    assert plan["buckets"]["O"]["count"] == 1 and plan["buckets"]["O"]["open_balance_count"] == 1
    assert plan["counts"] == dict(eligible=62, already_done=0, refused=1, admitted_plus_refused_a=62)
    c = plan["completeness"]["NGN"]
    assert c["a_equals_admitted_plus_refused"] and c["approved_open_equals_a_plus_u"]
    assert (c["a_count"], c["u_count"], c["approved_open_count"]) == (62, 3, 65)
    assert counts(engine, env.tenant_id) == dict(claim_liabilities=0, historical_claim_receipts=0, reclass_journals=0)


# --- p2, p3 -------------------------------------------------------------------------------------------------------


def test_p2_apply_admits_62_posts_one_reclass_and_the_gate_ties(engine, tmp_path):
    env, scene = census_scene(engine, tmp_path)
    result = scene.apply()
    assert result["admitted"] == 62 and not result["replayed"]
    with Session(engine) as db:
        liabilities = list(db.scalars(sa.select(ClaimLiability).where(ClaimLiability.tenant_id == env.tenant_id)))
        assert len(liabilities) == 62
        assert {(x.origin, x.liability_account_ref, x.status) for x in liabilities} == {
            ("historical", str(env.accounts["2045"]), "open")
        }
        assert sum(x.amount for x in liabilities) == D("529205.10")
        assert db.scalar(sa.select(sa.func.count()).select_from(HistoricalClaimReceipt)
                         .where(HistoricalClaimReceipt.tenant_id == env.tenant_id)) == 62  # fmt: skip
        (journal,) = db.scalars(
            sa.select(JournalEntry).where(
                JournalEntry.tenant_id == env.tenant_id, JournalEntry.source_document_kind == "claim_family_reclass"
            )
        )
        assert journal.status == JournalStatus.POSTED and journal.source_owner == "erp.migration"
        lines = list(db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal.id)))
        by = {}
        for x in lines:
            by.setdefault(x.account_code, []).append(x)
        assert sum(x.debit for x in by["2030"]) == D("70305.10") and sum(x.debit for x in by["2110"]) == D("458900.00")
        assert sum(x.credit for x in by["2045"]) == D("529205.10") and len(by["2045"]) == 32
        assert "2040" not in by
        assert claims_payable_total(db, tenant_id=env.tenant_id, liability_account_ref=str(env.accounts["2045"])) == D(
            "529205.10"
        )
        assert ledger_check.control_state(db, env.tenant_id)["2045"]["ok"]
        assert register._claims_reclass_matches_admissions(db, env.tenant_id) == []
    assert gl(engine, env, "2045") == D("-529205.10")


def test_p3_replay_is_a_no_op_and_a_different_plan_after_the_reclass_is_refused(engine, tmp_path):
    env, scene = census_scene(engine, tmp_path)
    plan = scene.plan()
    first = scene.apply(plan)
    before = counts(engine, env.tenant_id)
    again = scene.run(apply=True, digest=plan["digest"])  # the same reviewed plan
    assert again["replayed"] and again["journal_id"] == first["journal_id"] and again["admitted"] == 0
    assert counts(engine, env.tenant_id) == before
    scene.claim(net="40.00", carried="2110")  # a different plan: one more admissible claim
    scene.build()
    new_plan = scene.plan()
    with pytest.raises(ClaimsRefusal) as e:
        scene.run(apply=True, digest=new_plan["digest"])
    assert e.value.reason == "historical_claim_window_closed"
    assert counts(engine, env.tenant_id) == before


# --- p4 -----------------------------------------------------------------------------------------------------------


def test_p4_a_failure_at_the_last_admission_or_at_the_reclass_leaves_nothing(engine, tmp_path, monkeypatch):
    env, scene = census_scene(engine, tmp_path)
    plan = scene.plan()
    real = core.admit_historical_claim_liability
    calls = []

    def fail_last(*a, **kw):
        calls.append(1)
        if len(calls) == 62:
            raise ExpensesError("forced failure at the last admission")
        return real(*a, **kw)

    monkeypatch.setattr(core, "admit_historical_claim_liability", fail_last)
    with pytest.raises(ClaimsRefusal):
        scene.run(apply=True, digest=plan["digest"])
    assert len(calls) == 62
    assert counts(engine, env.tenant_id) == dict(claim_liabilities=0, historical_claim_receipts=0, reclass_journals=0)
    monkeypatch.setattr(core, "admit_historical_claim_liability", real)

    def fail_post(*a, **kw):
        raise ClaimsRefusal("forced_failure_at_the_reclass_post")

    monkeypatch.setattr(core.reclass, "post", fail_post)
    with pytest.raises(ClaimsRefusal) as e:
        scene.run(apply=True, digest=plan["digest"])
    assert e.value.reason == "forced_failure_at_the_reclass_post"
    assert counts(engine, env.tenant_id) == dict(claim_liabilities=0, historical_claim_receipts=0, reclass_journals=0)
    assert gl(engine, env, "2045") == 0


# --- p5, p8 -------------------------------------------------------------------------------------------------------


def refusal_scene(engine, tmp_path, accepted=True):
    env = world(engine)
    scene = Scene(engine, env, tmp_path)
    ids = {}
    ids["ok"] = scene.claim(net="100.00", carried="2110")
    ids["claim_net_payable_missing"] = scene.claim(net_null=True, dest=False)
    ids["legacy_claim_gl_unattributable"] = scene.claim(
        net="100.00", credit_lines=[("6000", "90.00", "0"), ("2110", "0", "90.00")]
    )
    ids["claim_carried_split"] = scene.claim(
        net="100.00", credit_lines=[("6000", "100.00", "0"), ("2110", "0", "60.00"), ("2030", "0", "40.00")]
    )
    ids["claim_carried_outside_family"] = scene.claim(
        net="100.00", credit_lines=[("6000", "100.00", "0"), ("2000", "0", "100.00")]
    )
    ids["reimbursement_not_posted"] = scene.claim(net="100.00", paid="10.00")
    ids["settlements_sum_mismatch"] = scene.claim(
        net="100.00",
        paid="10.00",
        credit_lines=[("6000", "100.00", "0"), ("2110", "0", "100.00")],
        reimb=[("2110", "5.00", "0"), ("1000", "0", "5.00")],
    )
    ids["claim_advance_unproven"] = scene.claim(net="100.00", advance="5.00")
    ids["claim_currency_unsupported"] = scene.claim(net="100.00", currency="USD")
    ids["claim_without_employee"] = scene.claim(net="100.00", employee=None, dest=False)
    ids["destination_claim_missing"] = scene.claim(net="100.00", dest=False)
    ids["journal_undated"] = scene.claim(net="100.00", undated=True)
    ids["journal_straddles_history_cutoff"] = scene.claim(net="100.00", ledger_day="2025-12-20")
    ids["destination_journal_missing"] = scene.claim(net="100.00", dest_journal=False)
    ids["owner_refused:claim_already_accepted"] = scene.claim(net="100.00")
    ids["held2040"] = scene.claim(net="100.00", credit_lines=[("6000", "100.00", "0"), ("2040", "0", "90.00")])
    scene.build()
    if not accepted:
        return env, scene, ids
    with Session(engine) as db:  # a liability the claim already has (accepted in v2)
        db.add(
            ClaimLiability(
                tenant_id=env.tenant_id, claim_id=ids["owner_refused:claim_already_accepted"],
                claimant_party_id=db.scalar(sa.select(ExpenseClaim.claimant_party_id).where(ExpenseClaim.id == ids["owner_refused:claim_already_accepted"])),
                currency_code="NGN", minor_units=2, liability_account_ref=str(env.accounts["2045"]), origin="accepted",
                input_vat_mode="none", acceptance_reference="acc-1", accepted_by="x", accepted_at=NOW, amount=D("100.00"),
                amount_settled=D(0), amount_adjusted=D(0), status="open",
            )
        )  # fmt: skip
        db.commit()
    return env, scene, ids


def test_p5_every_refusal_is_refused_not_admitted_present_in_the_receipt_and_loads_as_e37(engine, tmp_path):
    env, scene, ids = refusal_scene(engine, tmp_path)
    plan = scene.plan()
    refused = {d["legacy_claim_id"]: d["reason"] for d in plan["detail"] if d["outcome"] == "refused"}
    for reason, cid in ids.items():
        if reason in ("ok", "held2040"):
            continue
        assert refused[str(cid)] == reason, reason
    assert refused[str(ids["held2040"])] == "legacy_claim_gl_unattributable"
    assert str(ids["ok"]) not in refused
    elig = [d for d in plan["detail"] if d["outcome"] == "eligible"]
    assert [d["legacy_claim_id"] for d in elig] == [str(ids["ok"])]
    # apply on a tenant where the owner is empty (the refused claims do not block it; the formerly accepted one is eligible)
    (tmp_path / "b").mkdir()
    env, scene, ids = refusal_scene(engine, tmp_path / "b", accepted=False)
    plan = scene.plan()
    assert {d["legacy_claim_id"] for d in plan["detail"] if d["outcome"] == "eligible"} == {
        str(ids["ok"]),
        str(ids["owner_refused:claim_already_accepted"]),
    }
    result = scene.apply(plan)
    receipt = core.apply_receipt(result, mode="apply", attribution_sha256="a" * 64, fragment_sha256="b" * 64)
    keys = {i["key"]: i for i in receipt["items"]}
    assert f"claim:{ids['claim_carried_split']}" in keys and all(i["class"] == "E37" for i in receipt["items"])
    names = [f"Zed Claimant {p.hex[:8]}" for p in scene.employees.values()]
    assert not any(n in json.dumps(receipt) for n in names)
    facts = register.from_claims_opening(json.loads(json.dumps(receipt)))
    with Session(engine) as db:
        counts_ = register.reconcile(
            db, env.tenant_id, facts, loaded_sources={"claims_opening"}, receipt_sha="c" * 64, apply=True
        )
        db.commit()
        rows = list(
            db.execute(
                sa.text("SELECT class_code, legacy_key, detail FROM erp_migration_exceptions WHERE tenant_id = :t"),
                {"t": env.tenant_id},
            )
        )
    assert counts_["inserted_open"] == len(facts) and {r[0] for r in rows} == {"E37"}
    assert {r[1] for r in rows} == set(keys) and all(isinstance(r[2]["gl_signed_minor"], int) for r in rows)
    # the 2040-carried refusal is in the 2040 fragment, keyed by the LEGACY employee id (not the claimant party id)
    fragment = (
        result["fragment"]
        if "fragment" in result
        else core.fragment_2040(result["admitted_items"], result["items"], D(0))
    )
    held = {h["legacy_key"]: h for h in fragment["held_2040"]}
    legacy_employee = next(
        r["employee_id"] for r in scene.rows["expense_claim"] if r["claim_id"] == str(ids["held2040"])
    )
    assert held[f"claim:{ids['held2040']}"] == dict(
        kind="employee",
        key=legacy_employee,
        class_code="E37",
        reason="legacy_claim_gl_unattributable",
        legacy_key=f"claim:{ids['held2040']}",
        amount="-90.00",
    )
    assert legacy_employee not in {str(p) for p in scene.employees.values()}


def test_p8_a_split_or_outside_family_claim_is_refused(engine, tmp_path):
    _env, scene, _ids = refusal_scene(engine, tmp_path)
    refused = {d["reason"] for d in scene.plan()["detail"] if d["outcome"] == "refused"}
    assert {"claim_carried_split", "claim_carried_outside_family"} <= refused


# --- p6 -----------------------------------------------------------------------------------------------------------


def test_p6_a_claim_recorded_on_2040_is_admitted_with_a_dr_2040_line_and_a_fragment_keyed_by_the_legacy_employee(
    engine, tmp_path
):
    env = world(engine)
    scene = Scene(engine, env, tmp_path)
    employee = scene.employee()
    cid = scene.claim(net="250.00", carried="2040", employee=employee)
    scene.claim(net="10.00", carried="2110")
    scene.build()
    result = scene.apply()
    with Session(engine) as db:
        journal = reclass.posted_reclass(db, env.tenant_id)
        assert core.journal_dr(db, env.tenant_id, journal.id, env.accounts["2040"]) == D("250.00")
        items = result["admitted_items"]
        fragment = core.fragment_2040(items, result["items"], D("250.00"))
    assert fragment["reclass_2040"] == [dict(key=employee, amount="250.00")] and fragment["held_2040"] == []
    assert str(scene.employees[employee]) != employee and gl(engine, env, "2040") == D("250.00") - D("250.00") + D(
        "250.00"
    )
    assert str(cid) in {i.legacy_claim_id for i in items}


# --- p7 -----------------------------------------------------------------------------------------------------------


def test_p7_the_cutoff_evidence(engine, tmp_path):
    env = world(engine)
    scene = Scene(engine, env, tmp_path)
    old = scene.claim(net="80.00", journal_day="2025-12-20", dest_journal=False)
    straddle = scene.claim(net="60.00", ledger_day="2025-12-20")
    scene.build()
    plan = scene.plan()
    by = {d["legacy_claim_id"]: d for d in plan["detail"]}
    assert by[str(old)]["outcome"] == "eligible"  # dated 2025-12-20, ledger <= cutoff, opening posted, absent from v2
    assert by[str(straddle)]["reason"] == "journal_straddles_history_cutoff"
    # the same claim with no opening is refused
    env2 = world(engine)
    scene2 = Scene(engine, env2, tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, opening=False)
    old2 = scene2.claim(net="80.00", journal_day="2025-12-20", dest_journal=False)
    scene2.build()
    (d,) = scene2.plan()["detail"]
    assert d["legacy_claim_id"] == str(old2) and d["reason"] == "cutoff_opening_not_posted"


# --- p9 -----------------------------------------------------------------------------------------------------------


def test_p9_a_second_tenants_claims_are_invisible_and_nothing_crosses(engine, tmp_path):
    env = world(engine)
    scene = Scene(engine, env, tmp_path)
    mine = scene.claim(net="100.00")
    env2 = world(engine)
    scene.claim(net="999.00", tenant=env2.tenant_id)  # a claim of another organization in the same snapshot
    scene.build()
    plan = scene.plan()
    assert [d["legacy_claim_id"] for d in plan["detail"]] == [str(mine)] and plan["buckets"]["A"]["sum"] == "100.00"
    scene.apply(plan)
    assert counts(engine, env2.tenant_id) == dict(claim_liabilities=0, historical_claim_receipts=0, reclass_journals=0)
    assert counts(engine, env.tenant_id) == dict(claim_liabilities=1, historical_claim_receipts=1, reclass_journals=1)
    with Session(engine) as db:
        (journal,) = db.scalars(
            sa.select(JournalEntry).where(
                JournalEntry.source_document_kind == "claim_family_reclass",
                JournalEntry.tenant_id.in_([env.tenant_id, env2.tenant_id]),
            )
        )
        assert journal.tenant_id == env.tenant_id
        assert {
            x.tenant_id
            for x in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal.id))
        } == {env.tenant_id}


# --- p10 ----------------------------------------------------------------------------------------------------------


def test_p10_an_existing_non_plan_liability_refuses_the_apply(engine, tmp_path):
    env, scene, _ids = refusal_scene(engine, tmp_path)  # holds an owner liability that is not this plan's
    assert not core.owner_state_empty(Session(engine), env.tenant_id)
    plan = scene.plan()
    with pytest.raises(ClaimsRefusal) as e:
        scene.run(apply=True, digest=plan["digest"])
    assert e.value.reason == "claims_owner_state_not_empty"


# --- p11, p12 -----------------------------------------------------------------------------------------------------


def test_p11_the_gate(engine, tmp_path):
    env = world(engine)
    scene = Scene(engine, env, tmp_path)
    scene.claim(net="300.00", carried="2110")
    scene.build()
    with Session(engine) as db:  # no receipts: 0 == 0, no check reason
        assert ledger_check.control_state(db, env.tenant_id)["2045"]["ok"]
        assert register._claims_reclass_matches_admissions(db, env.tenant_id) == []
    # a historical receipt written directly through the owner, with no reclass
    from dotmac_expenses import admit_historical_claim_liability
    from dotmac_kernel.cache import TenantScope

    source = sa.create_engine(f"sqlite:///{scene.path}")
    with source.connect() as src, Session(engine) as db:
        verifier = ClaimOpeningVerifier(src, scene.manifest(), CUTOFF, OP)
        scope = TenantScope(env.tenant_id)
        evidence = verifier.evidence(scope=scope, claim_id=UUID(scene.claims[0]), destination=db)
        admit_historical_claim_liability(
            db, scope=scope, command=verifier.command(evidence), verifier=verifier, idempotency_key="direct-1",
            idempotency_expires_at=None, admitted_at=NOW,
        )  # fmt: skip
        db.commit()
    source.dispose()
    with Session(engine) as db:
        state = ledger_check.control_state(db, env.tenant_id)["2045"]
        assert not state["ok"] and state["reason"] == "control_not_tied:2045"
        assert register._claims_reclass_matches_admissions(db, env.tenant_id) == ["claims_reclass_matches_admissions"]
    # after the apply of the same facts: the receipt exists, so the full flow is run on a fresh scene
    env2 = world(engine)
    scene2 = Scene(engine, env2, tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
    scene2.claim(net="300.00", carried="2110")
    scene2.build()
    scene2.apply()
    with Session(engine) as db:
        assert ledger_check.control_state(db, env2.tenant_id)["2045"]["ok"]
        assert register._claims_reclass_matches_admissions(db, env2.tenant_id) == []


def test_p12_the_2045_positions_equal_the_attribution_receipt_and_the_holding_has_no_items(engine, tmp_path):
    env, scene = census_scene(engine, tmp_path)
    result = scene.apply()
    attribution, _sha = attr.parse_gl_attribution(json.dumps(core.attribution_2045(result["admitted_items"])).encode())
    with Session(engine) as db:
        sub = positions.sub_2045(db, env.tenant_id)
        gl_total = positions.gl_net(db, env.tenant_id, env.accounts["2045"])
    assert {(p.key): v for (p, _c), v in sub.items()} == {p.key: v for p, v in attribution.gl.items()}
    assert all(p.kind == "employee" for p in attribution.gl) and len(sub) == 32
    items = attr.itemise(
        attribution=attribution, sub={p: v for (p, _c), v in sub.items()}, gl_total=gl_total,
        sub_total=sum(sub.values(), D(0)), holding_account_code="2902",
    )  # fmt: skip
    assert items == [] and gl_total == sum(sub.values(), D(0))


# --- p13, p14 -----------------------------------------------------------------------------------------------------


def test_p13_a_wrong_expenses_version_or_head_is_refused(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_installed_version", lambda: "0.1.0a1")
    monkeypatch.setenv("LEGACY_DATABASE_URL", "postgresql://x/legacy")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://x/dest")
    args = SimpleNamespace(
        mode="plan", tenant_id=str(uuid4()), window_id=WINDOW, operator_ref=OP, capture_date="2026-01-12",
        reviewed_plan_sha256=None, snapshot_manifest=None, snapshot_sha256=None, cutoff_receipt=None,
        cutoff_receipt_sha256=None, apply_receipt=None,
    )  # fmt: skip
    with pytest.raises(ClaimsRefusal) as e:
        cli._run(args)
    assert e.value.reason == "released_expenses_version_required"
    monkeypatch.setattr(cli, "_stamped_heads", lambda db: {"ex_0001_expenses"})
    env = world(engine)
    with Session(engine) as db, pytest.raises(ClaimsRefusal) as e:
        cli._preflight(db, env.tenant_id, lambda *a: None, ValueError)
    assert e.value.reason == "released_expenses_version_required"
    monkeypatch.undo()
    with Session(engine) as db:
        cli._preflight(db, env.tenant_id, lambda *a: None, ValueError)  # the real head includes ex_0002


def test_p14_the_verifier_cannot_write(engine, tmp_path):
    from dotmac_expenses import InvalidCommand

    class Writer(ClaimOpeningVerifier):
        def verify(self, *, scope, command, destination):
            evidence = super().verify(scope=scope, command=command, destination=destination)
            destination.add(
                Party(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    party_type=PartyType.person,
                    display_name="w",
                    email=f"{uuid4().hex}@w.test",
                )
            )
            destination.flush()
            return evidence

    env = world(engine)
    scene = Scene(engine, env, tmp_path)
    scene.claim(net="100.00")
    scene.build()
    plan = scene.plan()
    with pytest.raises(ClaimsRefusal) as e:
        scene.run(apply=True, digest=plan["digest"], verifier_cls=Writer)
    assert isinstance(e.value.__cause__, InvalidCommand) and "must not write" in str(e.value.__cause__)
    assert counts(engine, env.tenant_id) == dict(claim_liabilities=0, historical_claim_receipts=0, reclass_journals=0)
