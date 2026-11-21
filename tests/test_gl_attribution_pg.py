# ruff: noqa: E501
"""GL attribution receipts (2000 / 2040 / 1410) on real PostgreSQL: a synthetic legacy snapshot in its own database, the
real destination (migrations, accounting, posting bridge), the real producer and the real holdings plan and apply.

GL-ATTRIBUTION-RECEIPTS-SPEC section 7: p1 (2000), p1a (K3), p2 (2040), p2a (K2), p3 (1410), p4 (staleness), p5
(integrity), p6 (register binding), p7 (tenant isolation), p8 (keys) and the producer CLI. Hosted CI only
(ERP_TEST_POSTGRES_URL), like test_holdings_pg.
"""

import hashlib
import json
import os
import stat
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import CreateJournal, JournalKind, JournalLineInput, SourceIdentity
from dotmac_kernel.cache import TenantScope
from sqlalchemy.orm import Session
from test_holdings_pg import (
    OP_A,
    billing,
    holding_apply,
    make_spec,
    net,
    seed_admissions,
    seed_gl,
    seed_register,
    t1,
    world,
)
from test_journals_pg import NOW, _registries, seed  # noqa: F401  (hosted-Postgres fixtures)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.holdings import apply as applying
from erp_v2.holdings import cli as holdings_cli
from erp_v2.holdings import positions
from erp_v2.holdings.attribution import Party
from erp_v2.holdings.errors import HoldingRefused
from erp_v2.migration_ledger import attribution_cli as acli
from erp_v2.migration_ledger import attribution_rules as rules
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import LedgerRefused
from erp_v2.migration_ledger.rounding_verify import ROUNDING_KIND

D = Decimal
CUTOFF, CAPTURE = date(2025, 12, 31), date(2026, 1, 20)
SHA = "b" * 64


# --- the synthetic legacy snapshot ---------------------------------------------------------------------------------

LEGACY_DDL = """
CREATE SCHEMA gl; CREATE SCHEMA ap; CREATE SCHEMA payroll; CREATE SCHEMA expense;
CREATE TABLE gl.account (account_id uuid PRIMARY KEY, account_code text);
CREATE TABLE gl.journal_entry (journal_entry_id uuid PRIMARY KEY, status text, posting_date date,
  reversal_journal_id uuid, correlation_id text);
CREATE TABLE gl.posted_ledger_line (ledger_line_id serial PRIMARY KEY, journal_entry_id uuid, account_id uuid,
  debit_amount numeric(20,6), credit_amount numeric(20,6), posting_date date, source_document_type text,
  source_document_id uuid);
CREATE TABLE ap.supplier_invoice (invoice_id uuid PRIMARY KEY, supplier_id uuid, currency_code text,
  exchange_rate numeric, functional_currency_amount numeric(20,6), journal_entry_id uuid);
CREATE TABLE ap.supplier_payment (payment_id uuid PRIMARY KEY, supplier_id uuid, journal_entry_id uuid);
CREATE TABLE ap.payment_allocation (payment_id uuid, invoice_id uuid, allocated_amount numeric(20,6),
  discount_taken numeric(20,6) DEFAULT 0, allocation_date date);
CREATE TABLE payroll.salary_slip (slip_id uuid PRIMARY KEY, employee_id uuid, journal_entry_id uuid,
  payroll_entry_id uuid);
CREATE TABLE expense.expense_claim (claim_id uuid PRIMARY KEY, employee_id uuid, journal_entry_id uuid,
  reimbursement_journal_id uuid);
"""


class Legacy:
    """A throwaway legacy database next to the test database (created and dropped through the admin connection)."""

    def __init__(self):
        raw = os.environ.get("ERP_TEST_POSTGRES_URL")
        if not raw:
            pytest.skip("ERP_TEST_POSTGRES_URL is required")
        assert os.environ.get("GITHUB_ACTIONS") == "true"
        self.admin_url = sa.make_url(raw)
        self.name = f"glattr_legacy_{uuid4().hex}"
        admin = sa.create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.exec_driver_sql(f'CREATE DATABASE "{self.name}"')
        admin.dispose()
        self.url = self.admin_url.set(database=self.name)
        self.engine = sa.create_engine(self.url)
        with self.engine.begin() as c:
            for stmt in LEGACY_DDL.split(";"):
                if stmt.strip():
                    c.exec_driver_sql(stmt)
        self.rows: list[tuple[str, date, str, Decimal]] = []  # (journal, day, code, net) for the v2 side

    def drop(self):
        self.engine.dispose()
        admin = sa.create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
        admin.dispose()

    def sql(self, text, **params):
        with self.engine.begin() as c:
            c.execute(sa.text(text), params)

    def account(self, account_id, code):
        self.sql("INSERT INTO gl.account VALUES (:a, :c)", a=account_id, c=code)

    def journal(self, jid, day, lines, *, status="POSTED", reversal=None, corr=None):
        """``lines``: (account id, code or None, net debit - credit, source type, source id)."""
        self.sql("INSERT INTO gl.journal_entry VALUES (:j, :s, :d, :r, :c)", j=jid, s=status, d=day, r=reversal, c=corr)
        for account_id, code, amount, stype, sid in lines:
            amount = D(amount)
            self.sql(
                "INSERT INTO gl.posted_ledger_line (journal_entry_id, account_id, debit_amount, credit_amount, "
                "posting_date, source_document_type, source_document_id) VALUES (:j, :a, :dr, :cr, :d, :t, :i)",
                j=jid, a=account_id, dr=max(amount, D(0)), cr=max(-amount, D(0)), d=day, t=stype, i=sid,
            )  # fmt: skip
            if code is not None:
                self.rows.append((str(jid), day, code, amount))

    def total(self, code, *, upto=None, after=None):
        return sum(
            (
                n
                for _j, d, c, n in self.rows
                if c == code and (upto is None or d <= upto) and (after is None or d > after)
            ),
            D(0),
        )


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


@pytest.fixture
def legacy():
    lg = Legacy()
    yield lg
    lg.drop()


def post_v2(engine, env, day, code, amount, owner, kind, offset="3100"):
    """One balanced v2 journal (``amount`` debit - credit on ``code``) with the given owner and document kind."""
    amount = D(amount)
    lines = ((code, amount), (offset, -amount))
    with Session(engine) as db:
        scope = TenantScope(env.tenant_id)
        journal = accounting.create_journal(
            db, scope=scope,
            command=CreateJournal(
                number=f"GA-{uuid4().hex[:12]}", kind=JournalKind.STANDARD, entry_date=day, posting_date=day,
                currency_code="NGN", exchange_rate=D(1), description="attr", reference="attr",
                source=SourceIdentity(owner=owner, document_kind=kind, document_id=f"d-{uuid4().hex}", version="1", fingerprint="f" * 64),
                lines=tuple(
                    JournalLineInput(env.accounts[c], max(n, D(0)), max(-n, D(0)), None) for c, n in lines
                ),
            ),
            idempotency_key=f"t:{uuid4()}", idempotency_expires_at=None, recorded_at=NOW,
        )  # fmt: skip
        accounting.post_journal(
            db, scope=scope, journal_id=journal.id, approval_reference="t", posted_by="t",
            idempotency_key=f"p:{uuid4()}", idempotency_expires_at=None, posted_at=NOW,
        )  # fmt: skip
        db.commit()


def mirror_v2(engine, env, lg, code, *, day_open=date(2026, 1, 2), day_legacy=date(2026, 1, 3)):
    """The destination as B-9 and the importer leave it: the opening at the legacy cumulative balance to the cutoff and
    the legacy-owner FY2026 journals (one journal, the legacy total after the cutoff)."""
    opening, later = lg.total(code, upto=CUTOFF), lg.total(code, after=CUTOFF)
    if opening:
        post_v2(engine, env, day_open, code, opening, "erp.opening", "opening")
    if later:
        post_v2(engine, env, day_legacy, code, later, "legacy-erp", "journal")


def legacy_world(engine, lg):
    return world(engine)


def produce(engine, env, lg, tmp_path, *, sources=None, tenant=None):
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    shas = {"legacy_snapshot": "1" * 64, "cutoff_receipt": "2" * 64, "p_attr_receipt": rules.P_ATTR_RECEIPT_SHA256}
    with lg.engine.connect() as conn, conn.begin():
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        with applying.read_only_session(engine) as db:
            try:
                out = acli.produce(
                    conn, db, tenant=tenant or env.tenant_id, capture=CAPTURE, cutoff=CUTOFF, shas=shas,
                    sources=sources or acli.Sources(),
                )  # fmt: skip
            finally:
                db.rollback()
    return out


def written(out, tmp_path):
    return acli.write_outputs(out, tmp_path / "out"), tmp_path / "out"


def receipt(out, code):
    return out["receipts"][code].body


def ids(n):
    return [uuid4() for _ in range(n)]


# --- p1 / p2 / p3: the whole flip over one synthetic legacy snapshot -------------------------------------------------


def build_flip_legacy(env, lg):
    a2000, a2040, a1410, bank = (env.accounts[c] for c in ("2000", "2040", "1410")), None, None, uuid4()
    a2000, a2040, a1410 = env.accounts["2000"], env.accounts["2040"], env.accounts["1410"]
    f = SimpleNamespace()
    f.SA, f.SB, f.SC = ids(3)
    (f.IA1, f.IA2, f.IA3, f.IB1, f.IC1) = ids(5)
    (f.JA1, f.JA2, f.JA3, f.JR3, f.JB1, f.JC1, f.JP, f.JPA, f.JS, f.OR1, f.OR2) = (uuid4() for _ in range(11))
    f.PA, f.PB, f.PA2, f.ORP1, f.ORP2 = ids(5)
    d = date
    for inv, sup, amt, j in (
        (f.IA1, f.SA, 100, f.JA1), (f.IA2, f.SA, 50, f.JA2), (f.IA3, f.SA, 20, f.JA3),
        (f.IB1, f.SB, 80, f.JB1), (f.IC1, f.SC, 30, f.JC1),
    ):  # fmt: skip
        lg.sql("INSERT INTO ap.supplier_invoice VALUES (:i, :s, 'NGN', NULL, :a, :j)", i=inv, s=sup, a=amt, j=j)
    lg.sql("INSERT INTO ap.supplier_payment VALUES (:p, :s, :j)", p=f.PA, s=f.SA, j=f.JP)
    lg.sql("INSERT INTO ap.supplier_payment VALUES (:p, :s, :j)", p=f.PB, s=f.SB, j=f.JP)
    lg.sql("INSERT INTO ap.supplier_payment VALUES (:p, :s, NULL)", p=f.PA2, s=f.SA)
    for pay, inv, amt in (
        (f.PA, f.IA2, 15),
        (f.PB, f.IB1, 25),
        (f.ORP1, f.IA2, 5),
        (f.ORP2, f.IA2, 1),
        (f.ORP2, f.IB1, 2),
    ):
        lg.sql("INSERT INTO ap.payment_allocation VALUES (:p, :i, :a, 0, '2026-01-10')", p=pay, i=inv, a=amt)
    SI, SP = "SUPPLIER_INVOICE", "SUPPLIER_PAYMENT"
    lg.journal(f.JA1, d(2025, 12, 10), [(a2000, "2000", "-100", SI, f.IA1)])
    lg.journal(f.JA2, d(2026, 1, 5), [(a2000, "2000", "-50", SI, f.IA2)])
    lg.journal(f.JA3, d(2026, 1, 8), [(a2000, "2000", "-20", SI, f.IA3)], status="REVERSED", reversal=f.JR3)
    lg.journal(f.JR3, d(2026, 1, 9), [(a2000, "2000", "20", None, None)])  # the reversal inherits the owner
    lg.journal(f.JB1, d(2026, 1, 6), [(a2000, "2000", "-80", SI, f.IB1)])
    lg.journal(f.JC1, d(2026, 1, 7), [(a2000, "2000", "-30", SI, f.IC1)])
    lg.journal(f.JP, d(2026, 1, 10), [(a2000, "2000", "40", SP, f.PA)])  # two payments, one journal: shared
    lg.journal(f.JPA, d(2026, 1, 11), [(a2000, "2000", "10", SP, None), (bank, None, "-10", SP, f.PA2)])  # any line
    lg.journal(f.JS, d(2026, 1, 12), [(a2000, "2000", "7", "JOURNAL", None)])  # a STANDARD journal: unattributed
    lg.journal(f.OR1, d(2026, 1, 13), [(a2000, "2000", "5", SP, f.ORP1)])  # K3: allocations name one supplier
    lg.journal(f.OR2, d(2026, 1, 14), [(a2000, "2000", "3", SP, f.ORP2)])  # K3: two suppliers: no owner
    # 2040
    f.E1, f.E2, f.E3 = ids(3)
    f.PE1, f.CL1, f.CL2, f.CL0, f.SL1, f.SL2, f.SL3 = ids(7)
    (f.JPR, f.JCK, f.JRE, f.JC2, f.JD, f.JCO) = (uuid4() for _ in range(6))
    for slip, emp in ((f.SL1, f.E1), (f.SL2, f.E2), (f.SL3, f.E3)):
        lg.sql("INSERT INTO payroll.salary_slip VALUES (:s, :e, NULL, :p)", s=slip, e=emp, p=f.PE1)
    lg.sql("INSERT INTO expense.expense_claim VALUES (:c, :e, :j, :r)", c=f.CL1, e=f.E1, j=f.JCK, r=f.JRE)
    lg.sql("INSERT INTO expense.expense_claim VALUES (:c, :e, :j, NULL)", c=f.CL2, e=f.E2, j=f.JC2)
    lg.sql("INSERT INTO expense.expense_claim VALUES (:c, :e, :j, NULL)", c=f.CL0, e=f.E3, j=f.JCO)
    lg.journal(f.JCO, d(2025, 12, 20), [(a2040, "2040", "-25", "EXPENSE_CLAIM", None)])
    lg.journal(f.JPR, d(2026, 1, 12), [(a2040, "2040", "-600", "PAYROLL_ENTRY", f.PE1)])
    lg.journal(f.JCK, d(2026, 1, 13), [(a2040, "2040", "-100", "EXPENSE_CLAIM", None)])
    lg.journal(f.JRE, d(2026, 1, 14), [(a2040, "2040", "40", "EXPENSE_REIMBURSEMENT", f.CL1)])
    lg.journal(f.JC2, d(2026, 1, 15), [(a2040, "2040", "-50", "EXPENSE_CLAIM", None)])
    lg.journal(f.JD, d(2026, 1, 16), [(a2040, "2040", "100", "PAYROLL_DISBURSEMENT", None)])
    # 1410: 22 x Dr 37,500.00 and 2 x Cr 10,000.00 STANDARD journals with no document (net Dr 805,000.00)
    f.J1410 = []
    for i in range(24):
        j = uuid4()
        f.J1410.append(j)
        lg.journal(
            j,
            date(2025, 2, 17) + (date(2025, 3, 1) - date(2025, 2, 1)) * (i // 8),
            [(a1410, "1410", "37500" if i < 22 else "-10000", "JOURNAL", None)],
        )
    return f


def flip_env(engine, lg, tmp_path):
    """The destination before the producer: T1 posted (runbook F7 step 1), opening and legacy journals mirrored."""
    env = legacy_world(engine, lg)
    seed_admissions(engine, env)
    seed_register(engine, env, [("E35", "partition:C1:NGN", {})])
    seed_gl(engine, env, "1400", D("1530"))
    f = build_flip_legacy(env, lg)
    for code in ("2000", "2040", "1410"):
        mirror_v2(engine, env, lg, code)
    # S8: the claim-family reclass journal (Dr 2040 50.00)
    post_v2(engine, env, date(2026, 1, 4), "2040", D("50.00"), "erp.migration", "claim_family_reclass")
    spec = make_spec(tmp_path)
    env.spec_dict = spec
    env.tmp = tmp_path
    env.f = f
    env.spec = json.dumps({**spec, "tenant_id": str(env.tenant_id)}).encode()
    t1(engine, env)
    return env


def register_sources(engine, env, tmp_path, f, *, unpaid_body, itemised_body, held_claim_minor=-700, claim=True):
    """``register load`` of the AP-admission and claims sources (runbook F7 step 3), through the real adapters."""
    facts = register.from_import_itemised(itemised_body) + register.from_ap_admission(unpaid_body)
    with Session(engine) as db:
        register.reconcile(
            db, env.tenant_id, facts, loaded_sources={register.SRC_IMPORT, register.SRC_AP_ADMISSION},
            receipt_sha=SHA, apply=True,
        )  # fmt: skip
        if claim:  # what S8's from_claims_opening loads (E37 claim:<legacy id>, gl_signed_minor in the detail)
            db.add(
                m.MigrationException(
                    tenant_id=env.tenant_id, class_code="E37", legacy_key="claim:99", source_kind="claims_opening",
                    state="open", fingerprint=SHA, source_receipt_sha256=SHA,
                    detail={"gl_signed_minor": held_claim_minor},
                )
            )  # fmt: skip
        db.commit()


def sources_for(env, tmp_path, f, *, fragment_amounts=("30.00", "20.00")):
    unpaid = {"version": 1, "mode": "plan", "refusals": [{"invoice_id": str(f.IB1), "reason": "source_journal_status"}]}
    itemised = {
        "queue": [
            {
                "invoice_id": str(f.IC1),
                "reasons": ["total_mismatch_after_transform"],
                "lands": False,
                "legacy_total": "30",
            }
        ]
    }
    fragment = {
        "kind": "claims-2040-fragment", "version": 1, "currency": "NGN",
        "reclass_2040": [{"key": str(f.E1), "amount": fragment_amounts[0]}, {"key": str(f.E2), "amount": fragment_amounts[1]}],
        "held_2040": [{"kind": "employee", "key": str(f.E3), "class_code": "E37", "reason": "claim_refused", "legacy_key": "claim:99", "amount": "-7.00"}],
        "held_2040_unkeyed": 0,
    }  # fmt: skip
    frag_bytes = json.dumps(fragment, sort_keys=True).encode()
    s8 = json.dumps({"kind": "claims-opening", "fragment_2040_sha256": hashlib.sha256(frag_bytes).hexdigest()}).encode()
    return (
        unpaid,
        itemised,
        acli.Sources(
            import_itemised=json.dumps(itemised).encode(),
            unpaid_plan=json.dumps(unpaid).encode(),
            s8_receipt=s8,
            s8_fragment=frag_bytes,
        ),
    )


def spec_with_receipts(engine, env, shas_dir, out):
    """The holdings run-spec naming the three receipts and the manifest (runbook F7 step 4)."""
    spec = json.loads(json.dumps(env.spec_dict))

    def ref(path):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    spec["receipts"]["gl_attribution"] = {
        c: ref(shas_dir / f"gl-attribution-{c}-NGN.json") for c in ("2000", "2040", "1410")
    }
    spec["receipts"]["gl_attribution_manifest"] = ref(shas_dir / "gl-attribution-manifest.json")
    return json.dumps({**spec, "tenant_id": str(env.tenant_id)}).encode()


def items_of(plan, code):
    entry = next(e for e in plan["plan"]["codes"] if e["code"] == code)
    return {i["key"]: (i["class"], D(i["amount"])) for i in entry["items"]}


def test_p1_p2_p3_the_flip_itemises_every_difference_and_the_controls_tie_after_apply(engine, legacy, tmp_path):
    env = flip_env(engine, legacy, tmp_path)
    f = env.f
    unpaid, itemised, sources = sources_for(env, tmp_path, f)
    register_sources(engine, env, tmp_path, f, unpaid_body=unpaid, itemised_body=itemised)
    out = produce(engine, env, legacy, tmp_path, sources=sources)
    _shas, out_dir = written(out, tmp_path)
    for p in out_dir.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    env.spec = spec_with_receipts(engine, env, out_dir, out)
    env.readers = {
        "2000": lambda db, t, c, ccy: {(Party("supplier", str(f.SA)), "NGN"): D("-140")},
        "2040": lambda db, t, c, ccy: {(Party("employee", str(e)), "NGN"): D("-100") for e in (f.E1, f.E2, f.E3)},
        "1410": lambda db, t, c, ccy: {(Party("employee", "E1"), "NGN"): D("0.01")},
        "2045": lambda db, t, c, ccy: {},
    }
    plan = applying.run_plan(
        engine, env.spec, holdings_cli.load_receipts, billing(), planned_by=OP_A, owner_readers=env.readers
    )
    k = lambda kind, code, ident: f"{kind}:{code}:NGN:{ident}"  # noqa: E731
    # p1: 2000
    assert items_of(plan, "2000") == {
        k("held", "2000", f"invoice:{f.IB1}"): ("E02", D("-55")),  # supplier B: refused at admission, residual 55
        k("held", "2000", f"invoice:{f.IC1}"): ("E04", D("-30")),  # supplier C: POSTED in legacy, not landed
        k("partygap", "2000", str(f.SA)): ("E37", D("5")),
        k("partygap", "2000", str(f.SB)): ("E37", D("-25")),  # residual 55 differs from GL 80
        k("sharedjournal", "2000", str(f.JP)): ("E37", D("40")),
        k("gljournal", "2000", str(f.JS)): ("E37", D("7")),
        k("gljournal", "2000", str(f.OR2)): ("E37", D("3")),  # K3: two suppliers, no owner
    }
    # p2: 2040 (payroll run shared, claims per employee, S8 fragment per employee and its held claim)
    assert items_of(plan, "2040") == {
        k("held", "2040", "claim:99"): ("E37", D("-7")),
        k("partygap", "2040", str(f.E1)): ("E37", D("70")),
        k("partygap", "2040", str(f.E2)): ("E37", D("70")),
        k("partygap", "2040", str(f.E3)): ("E37", D("82")),
        k("sharedjournal", "2040", str(f.JPR)): ("E37", D("-600")),
        k("gljournal", "2040", str(f.JD)): ("E37", D("100")),
    }
    # p3: 1410, 24 journals without a document and the 0.01 loan record
    got = items_of(plan, "1410")
    journals = {key: v for key, v in got.items() if key.startswith("gljournal")}
    assert len(journals) == 24 and sum(v[1] for v in journals.values()) == D("805000")
    assert got[k("partygap", "1410", "E1")] == ("E37", D("-0.01"))
    for code in ("2000", "2040", "1410"):  # Σ items == X, by construction and by the plan's own check
        entry = next(e for e in plan["plan"]["codes"] if e["code"] == code)
        assert sum(D(i["amount"]) for i in entry["items"]) == D(entry["x"])
    holding_apply(engine, env, plan)
    assert (
        net(engine, env, "2000") == D("-140")
        and net(engine, env, "2040") == D("-300")
        and net(engine, env, "1410") == D("0.01")
    )
    with Session(engine) as db:
        keys = {
            r.legacy_key
            for r in db.scalars(sa.select(m.MigrationException).where(m.MigrationException.tenant_id == env.tenant_id))
        }
    assert k("held", "2000", f"invoice:{f.IB1}") in keys and "claim:99" in keys and f"invoice:{f.IC1}" in keys
    # the manifest carries the P-ATTR grouping, the K3 / K2 evidence and the sources
    manifest = json.loads((out_dir / "gl-attribution-manifest.json").read_text())
    assert manifest["p_attr_receipt_sha256"] == rules.P_ATTR_RECEIPT_SHA256
    assert manifest["codes"]["2000"]["held"] == 2 and manifest["codes"]["2040"]["shared"] == 1
    by_type = {(r["code"], r["source_document_type"]): r for r in manifest["by_source_type"]}
    assert by_type[("2040", "PAYROLL_ENTRY")]["journals_by_outcome"] == {"shared": 1}
    assert by_type[("2000", "SUPPLIER_PAYMENT")]["journals_by_rule"]["L:SUPPLIER_PAYMENT_ALLOC"] == 1
    assert by_type[("2040", "EXPENSE_CLAIM")]["journals_by_rule"] == {"J:claim": 3}


def test_p1_the_owner_rules_resolve_line_level_journal_level_any_line_and_reversal_owners(engine, legacy, tmp_path):
    env = flip_env(engine, legacy, tmp_path)
    f = env.f
    out = produce(engine, env, legacy, tmp_path)
    body = receipt(out, "2000")
    parties = {p["key"]: D(p["gl"]) for p in body["parties"]}
    # A: invoices -100 -50, the reversed -20/+20, the any-line payment +10 and the K3 orphan +5
    assert parties == {str(f.SA): D("-135"), str(f.SB): D("-80"), str(f.SC): D("-30")}
    unattributed = {u["journal_id"]: (D(u["amount"]), u["shared"]) for u in body["unattributed"]}
    assert unattributed == {str(f.JP): (D("40"), True), str(f.JS): (D("7"), False), str(f.OR2): (D("3"), False)}
    assert body["opening_residue"] == "0.00" and body["held"] == []
    rules_seen = {
        r["source_document_type"]: r["journals_by_rule"]
        for r in out["manifest"]["by_source_type"]
        if r["code"] == "2000"
    }
    assert rules_seen["-"] == {"J:ap_invoice_reversal": 1}  # the reversal inherits the document's owner
    assert rules_seen["SUPPLIER_INVOICE"] == {"J:ap_invoice+L:SUPPLIER_INVOICE": 5}  # journal AND line level agree
    assert rules_seen["SUPPLIER_PAYMENT"]["L:SUPPLIER_PAYMENT"] == 1  # the any-line rule (the bank line carries it)
    assert out["receipts"]["2040"].summary["shared"] == 1


def test_p1a_k3_an_orphan_payment_line_is_owned_only_through_allocations_naming_one_supplier(engine, legacy, tmp_path):
    env = legacy_world(engine, legacy)
    s1, s2 = ids(2)
    i1, i2, i3 = ids(3)
    orphan_one, orphan_two, orphan_unresolved = ids(3)
    j1, j2, j3 = ids(3)
    a2000 = env.accounts["2000"]
    for inv, sup in ((i1, s1), (i2, s1), (i3, s2)):
        legacy.sql("INSERT INTO ap.supplier_invoice VALUES (:i, :s, 'NGN', NULL, 10, NULL)", i=inv, s=sup)
    for pay, inv in (
        (orphan_one, i1),
        (orphan_one, i2),
        (orphan_two, i1),
        (orphan_two, i3),
        (orphan_unresolved, uuid4()),
    ):
        legacy.sql("INSERT INTO ap.payment_allocation VALUES (:p, :i, 1, 0, '2026-01-10')", p=pay, i=inv)
    for j, orphan in ((j1, orphan_one), (j2, orphan_two), (j3, orphan_unresolved)):
        legacy.journal(j, date(2026, 1, 10), [(a2000, "2000", "4", "SUPPLIER_PAYMENT", orphan)])
    mirror_v2(engine, env, legacy, "2000")
    body = receipt(produce(engine, env, legacy, tmp_path), "2000")
    assert body["parties"] == [{"kind": "supplier", "key": str(s1), "gl": "4.00"}]  # both invoices, one supplier
    # two suppliers: no owner; an allocation whose invoice does not exist: no owner (never guessed)
    assert {u["journal_id"] for u in body["unattributed"]} == {str(j2), str(j3)}


def test_p2a_k2_backfill_reclass_pairs_cancel_as_zero_units_and_every_other_case_takes_the_ordinary_rule(
    engine, legacy, tmp_path
):
    env = legacy_world(engine, legacy)
    a2040 = env.accounts["2040"]
    e1 = uuid4()
    claim_a, cl1 = ids(2)
    legacy.sql("INSERT INTO expense.expense_claim VALUES (:c, :e, :j, NULL)", c=cl1, e=e1, j=claim_a)
    day = date(2026, 1, 6)
    pre = "EXPENSE-RECLASS-BACKFILL-JE-"
    # 1. an owned claim journal and its reclass (cancels): excluded
    r1 = uuid4()
    legacy.journal(claim_a, day, [(a2040, "2040", "-10", "EXPENSE_CLAIM", None)])
    legacy.journal(r1, day, [(a2040, "2040", "10", "RECLASS", None)], corr=f"{pre}{claim_a}")
    # 2. an unowned reimbursement and its reclass (cancels): excluded, no holding item
    unowned, r2 = uuid4(), uuid4()
    legacy.journal(unowned, day, [(a2040, "2040", "4", "EXPENSE_REIMBURSEMENT", None)])
    legacy.journal(r2, day, [(a2040, "2040", "-4", "RECLASS", None)], corr=f"{pre}{unowned}")
    # 3. a reclass that differs from its source by 0.01: not paired, both ordinary (owned source, unowned reclass)
    s3, r3 = uuid4(), uuid4()
    legacy.journal(s3, day, [(a2040, "2040", "-6.00", "EXPENSE_REIMBURSEMENT", None)])
    legacy.journal(r3, day, [(a2040, "2040", "5.99", "RECLASS", None)], corr=f"{pre}{s3}")
    # 4. a source referenced by two reclasses: not paired
    s4, r4, r4b = uuid4(), uuid4(), uuid4()
    legacy.journal(s4, day, [(a2040, "2040", "-2", "EXPENSE_REIMBURSEMENT", None)])
    legacy.journal(r4, day, [(a2040, "2040", "1", "RECLASS", None)], corr=f"{pre}{s4}")
    legacy.journal(r4b, day, [(a2040, "2040", "1", "RECLASS", None)], corr=f"{pre}{s4}")
    # 5. a reclass naming a journal that is not posted: not paired
    s5, r5 = uuid4(), uuid4()
    legacy.journal(s5, day, [(a2040, "2040", "-3", "EXPENSE_REIMBURSEMENT", None)], status="DRAFT")
    legacy.journal(r5, day, [(a2040, "2040", "3", "RECLASS", None)], corr=f"{pre}{s5}")
    # 6. the correlation id is built from S's id by exact equality: a different id text is never parsed
    s6, r6 = uuid4(), uuid4()
    legacy.journal(s6, day, [(a2040, "2040", "-1", "EXPENSE_REIMBURSEMENT", None)])
    legacy.journal(r6, day, [(a2040, "2040", "1", "RECLASS", None)], corr=f"{pre}{str(s6).upper()}")
    mirror_v2(engine, env, legacy, "2040")
    out = produce(engine, env, legacy, tmp_path)
    body = receipt(out, "2040")
    unattributed = {u["journal_id"] for u in body["unattributed"]}
    assert not unattributed & {str(x) for x in (claim_a, r1, unowned, r2)}  # zero units: out of the receipt, no item
    assert body["parties"] == []  # the owned claim journal is part of a zero unit, so employee e1 receives nothing
    # not paired (0.01 apart; a source referenced twice; a non-posted source; a non-exact id text): the ordinary rule
    assert {str(x) for x in (s3, r3, s4, r4, r4b, s6, r6)} <= unattributed
    assert {str(s5), str(r5)} <= unattributed  # a non-posted source is not paired either
    summary = out["receipts"]["2040"].summary
    assert summary["k2_zero_units"] == 2 and summary["zero_unit_journals"] == 4
    assert sum(D(u["amount"]) for u in body["unattributed"]) == sum(
        D(x) for x in ("-6.00", "5.99", "-2", "1", "1", "-3", "3", "-1", "1")
    )


def test_p4_the_manifest_goes_stale_when_anything_posts_and_a_holding_journal_present_refuses(engine, legacy, tmp_path):
    env = flip_env(engine, legacy, tmp_path)
    f = env.f
    unpaid, itemised, sources = sources_for(env, tmp_path, f)
    register_sources(engine, env, tmp_path, f, unpaid_body=unpaid, itemised_body=itemised)
    out = produce(engine, env, legacy, tmp_path, sources=sources)
    _shas, out_dir = written(out, tmp_path)
    env.spec = spec_with_receipts(engine, env, out_dir, out)
    env.readers = {c: (lambda db, t, code, ccy: {}) for c in ("2000", "2040", "1410", "2045")}
    seed_gl(engine, env, "1400", D("1"))  # anything posted since production
    with pytest.raises(HoldingRefused) as e:
        applying.run_plan(
            engine, env.spec, holdings_cli.load_receipts, billing(), planned_by=OP_A, owner_readers=env.readers
        )
    assert e.value.code == "attribution_manifest_stale"
    # a holding journal already posted on a code: the producer refuses (the receipt is an input to the holding plan)
    post_v2(engine, env, date(2026, 1, 5), "2000", D("1"), "erp.migration", "migration_holding")
    with pytest.raises(LedgerRefused) as e2:
        produce(engine, env, legacy, tmp_path / "again")
    assert e2.value.code == "holding_already_posted"


def test_p5_integrity_faults_refuse_by_name(engine, legacy, tmp_path):
    env = legacy_world(engine, legacy)
    legacy.journal(uuid4(), date(2026, 1, 5), [(env.accounts["2000"], "2000", "-10", "JOURNAL", None)])
    mirror_v2(engine, env, legacy, "2000")
    produce(engine, env, legacy, tmp_path)  # clean
    post_v2(engine, env, date(2026, 1, 6), "2000", D("0.05"), "legacy-erp", "journal")  # an imported line altered in v2
    with pytest.raises(LedgerRefused) as e:
        produce(engine, env, legacy, tmp_path / "a")
    assert e.value.code == "legacy_import_differs:2000"
    native = world(engine)  # a native posting before the flip
    seed_gl(engine, native, "2040", D("-5"))
    with pytest.raises(LedgerRefused) as e:
        produce(engine, native, legacy, tmp_path / "b")
    assert e.value.code == "native_posting_before_flip:2040"
    straddle = world(engine)  # a straddle line on 2040
    post_v2(engine, straddle, date(2026, 1, 5), "2040", D("1"), "erp.migration", "migration_straddle")
    with pytest.raises(LedgerRefused) as e:
        produce(engine, straddle, legacy, tmp_path / "c")
    assert e.value.code == "straddle_on_control:2040"


def test_p5_a_rounding_journal_is_its_own_unattributed_row(engine, legacy, tmp_path):
    env = legacy_world(engine, legacy)
    legacy.journal(uuid4(), date(2026, 1, 5), [(env.accounts["2000"], "2000", "-10", "JOURNAL", None)])
    mirror_v2(engine, env, legacy, "2000")
    post_v2(engine, env, date(2026, 1, 6), "2000", D("0.01"), "erp.manual", ROUNDING_KIND)
    body = receipt(produce(engine, env, legacy, tmp_path), "2000")
    assert sorted(u["amount"] for u in body["unattributed"]) == ["-10.00", "0.01"]


def run_flip_receipts(engine, env, legacy, tmp_path, *, residual=None, claim=True):
    f = env.f
    unpaid, itemised, sources = sources_for(env, tmp_path, f)
    if residual is not None:  # a plan that records the legacy residual: gl_signed_minor is then stored in the register
        unpaid["refusals"][0]["residual"] = residual
        sources = acli.Sources(
            import_itemised=sources.import_itemised, unpaid_plan=json.dumps(unpaid).encode(),
            s8_receipt=sources.s8_receipt, s8_fragment=sources.s8_fragment,
        )  # fmt: skip
    register_sources(engine, env, tmp_path, f, unpaid_body=unpaid, itemised_body=itemised, claim=claim)
    out = produce(engine, env, legacy, tmp_path, sources=sources)
    _shas, out_dir = written(out, tmp_path)
    env.spec = spec_with_receipts(engine, env, out_dir, out)
    env.readers = {
        "2000": lambda db, t, c, ccy: {(Party("supplier", str(f.SA)), "NGN"): D("-140")},
        "2040": lambda db, t, c, ccy: {(Party("employee", str(e)), "NGN"): D("-100") for e in (f.E1, f.E2, f.E3)},
        "1410": lambda db, t, c, ccy: {(Party("employee", "E1"), "NGN"): D("0.01")},
        "2045": lambda db, t, c, ccy: {},
    }
    return lambda: applying.run_plan(
        engine, env.spec, holdings_cli.load_receipts, billing(), planned_by=OP_A, owner_readers=env.readers
    )


def test_p6_a_register_row_with_a_different_gl_signed_amount_refuses(engine, legacy, tmp_path):
    env = flip_env(engine, legacy, tmp_path)
    plan = run_flip_receipts(engine, env, legacy, tmp_path, residual="55.00")
    plan()  # clean
    with Session(engine) as db:
        row = db.scalar(
            sa.select(m.MigrationException).where(
                m.MigrationException.tenant_id == env.tenant_id,
                m.MigrationException.legacy_key == f"invoice:{env.f.IB1}",
            )
        )
        assert row.detail["gl_signed_minor"] == -5500  # the AP-admission adapter stored it
        row.detail = {**row.detail, "gl_signed_minor": -5400}
        db.commit()
    with pytest.raises(HoldingRefused) as e:
        plan()
    assert e.value.code == "held_amount_differs_from_register"
    with Session(engine) as db:  # and the S8 claim row (2040): its stored amount is enforced the same way
        claim = db.scalar(
            sa.select(m.MigrationException).where(
                m.MigrationException.tenant_id == env.tenant_id, m.MigrationException.legacy_key == "claim:99"
            )
        )
        invoice = db.scalar(
            sa.select(m.MigrationException).where(
                m.MigrationException.tenant_id == env.tenant_id,
                m.MigrationException.legacy_key == f"invoice:{env.f.IB1}",
            )
        )
        invoice.detail = {**invoice.detail, "gl_signed_minor": -5500}
        claim.detail = {**claim.detail, "gl_signed_minor": -699}
        db.commit()
    with pytest.raises(HoldingRefused) as e:
        plan()
    assert e.value.code == "held_amount_differs_from_register"


def test_p6_a_held_row_missing_from_the_register_refuses(engine, legacy, tmp_path):
    env = flip_env(engine, legacy, tmp_path)
    plan = run_flip_receipts(engine, env, legacy, tmp_path, claim=False)  # the S8 claim row was never loaded
    with pytest.raises(HoldingRefused) as e:
        plan()
    assert e.value.code == "held_not_in_register"


def test_p7_a_second_tenants_journals_are_never_attributed(engine, legacy, tmp_path):
    env = legacy_world(engine, legacy)
    legacy.journal(uuid4(), date(2026, 1, 5), [(env.accounts["2000"], "2000", "-10", "JOURNAL", None)])
    mirror_v2(engine, env, legacy, "2000")
    other = world(engine)
    seed_gl(engine, other, "2000", D("-999"))  # another tenant's native posting on ITS 2000
    seed_gl(engine, other, "2040", D("-1"))
    out = produce(engine, env, legacy, tmp_path)
    assert receipt(out, "2000")["unattributed"][0]["amount"] == "-10.00"
    assert out["receipts"]["2000"].summary["gl"] == "-10.00" and receipt(out, "2040")["unattributed"] == []
    assert net(engine, other, "2000") == D("-999")  # untouched, and invisible to the first tenant's receipt


def test_p8_an_open_2040_liability_for_a_non_employee_beneficiary_refuses_party_kind_unexpected(
    engine, legacy, tmp_path, monkeypatch
):
    env = legacy_world(engine, legacy)
    mirror_v2(engine, env, legacy, "2000")
    monkeypatch.setattr(positions, "sub_2040", lambda db, tenant: {(Party("external", "X1"), "NGN"): D("-5")})
    with pytest.raises(LedgerRefused) as e:
        produce(engine, env, legacy, tmp_path)
    assert e.value.code == "party_kind_unexpected"


def test_the_producer_is_pinned_to_the_p_attr_receipt_and_the_s8_fragment_must_be_bound(engine, legacy, tmp_path):
    env = flip_env(engine, legacy, tmp_path)
    f = env.f
    _unpaid, _itemised, sources = sources_for(env, tmp_path, f)
    with legacy.engine.connect() as conn, conn.begin(), applying.read_only_session(engine) as db:
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        shas = {"legacy_snapshot": "1" * 64, "cutoff_receipt": "2" * 64, "p_attr_receipt": "3" * 64}
        with pytest.raises(LedgerRefused) as e:
            acli.produce(
                conn, db, tenant=env.tenant_id, capture=CAPTURE, cutoff=CUTOFF, shas=shas, sources=acli.Sources()
            )
        assert e.value.code == "p_attr_receipt_not_pinned"
        shas["p_attr_receipt"] = rules.P_ATTR_RECEIPT_SHA256
        bad = acli.Sources(
            s8_receipt=b'{"fragment_2040_sha256": "' + b"0" * 64 + b'"}', s8_fragment=sources.s8_fragment
        )
        with pytest.raises(LedgerRefused) as e:
            acli.produce(conn, db, tenant=env.tenant_id, capture=CAPTURE, cutoff=CUTOFF, shas=shas, sources=bad)
        assert e.value.code == "s8_fragment_not_bound"


def test_a_fragment_off_by_a_cent_makes_the_s8_journal_unattributed_and_flags_it(engine, legacy, tmp_path):
    env = flip_env(engine, legacy, tmp_path)
    f = env.f
    _u, _i, sources = sources_for(env, tmp_path, f, fragment_amounts=("30.01", "20.00"))
    out = produce(engine, env, legacy, tmp_path, sources=sources)
    r = out["receipts"]["2040"]
    assert r.flags and r.flags[0]["reason"] == "fragment_mismatch"
    assert any(u["amount"] == "50.00" for u in r.body["unattributed"])
    assert out["manifest"]["codes"]["2040"]["flags"] == r.flags


def test_the_cli_reads_both_databases_read_only_pops_the_dsns_and_writes_only_0600_files(
    engine, legacy, tmp_path, monkeypatch
):
    env = flip_env(engine, legacy, tmp_path)
    f = env.f
    _u, _i, sources = sources_for(env, tmp_path, f)
    files = {}
    for name, raw in (
        ("itemised", sources.import_itemised),
        ("unpaid", sources.unpaid_plan),
        ("s8", sources.s8_receipt),
        ("frag", sources.s8_fragment),
    ):
        p = tmp_path / f"{name}.json"
        p.write_bytes(raw)
        files[name] = (p, hashlib.sha256(raw).hexdigest())
    with Session(engine) as db:
        dest_name = db.scalar(sa.text("SELECT current_database()"))
        db.rollback()
    snap = {
        "snapshot_sha256": "5" * 64, "source_database": legacy.name, "destination_database": dest_name,
        "candidate_revision": "a" * 40, "tenant_extent": [str(env.tenant_id)],
    }  # fmt: skip
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(snap))
    cutoff = tmp_path / "cutoff.json"
    cutoff.write_text(
        json.dumps({"kind": "execute", "dry_run": False, "complete": True, "history_cutoff": CUTOFF.isoformat()})
    )
    pattr = tmp_path / "p-attr.json"
    pattr.write_text("{}")
    out = tmp_path / "cli-out"
    out.mkdir()
    # the pinned receipt sha is the one the real receipt has: here the file differs, so the run refuses by name
    argv = [
        "--tenant-id", str(env.tenant_id), "--capture-date", CAPTURE.isoformat(), "--snapshot-manifest", str(mpath),
        "--snapshot-sha256", "5" * 64, "--cutoff-receipt", str(cutoff),
        "--cutoff-receipt-sha256", hashlib.sha256(cutoff.read_bytes()).hexdigest(), "--p-attr-receipt", str(pattr),
        "--p-attr-receipt-sha256", hashlib.sha256(pattr.read_bytes()).hexdigest(), "--out-dir", str(out),
        "--import-itemised", str(files["itemised"][0]), "--import-itemised-sha256", files["itemised"][1],
        "--unpaid-plan", str(files["unpaid"][0]), "--unpaid-plan-sha256", files["unpaid"][1],
        "--s8-receipt", str(files["s8"][0]), "--s8-receipt-sha256", files["s8"][1],
        "--s8-fragment", str(files["frag"][0]), "--s8-fragment-sha256", files["frag"][1],
    ]  # fmt: skip
    dest_url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", dest_url)
    monkeypatch.setenv("LEGACY_DATABASE_URL", legacy.url.render_as_string(hide_password=False))
    assert acli.main(argv) == 2 and list(out.iterdir()) == []  # not the pinned P-ATTR receipt: nothing written
    assert "MIGRATION_DATABASE_URL" not in os.environ and "LEGACY_DATABASE_URL" not in os.environ  # popped
    monkeypatch.setattr(rules, "P_ATTR_RECEIPT_SHA256", hashlib.sha256(pattr.read_bytes()).hexdigest())
    monkeypatch.setattr(acli.rules, "P_ATTR_RECEIPT_SHA256", hashlib.sha256(pattr.read_bytes()).hexdigest())
    monkeypatch.setenv("MIGRATION_DATABASE_URL", dest_url)
    monkeypatch.setenv("LEGACY_DATABASE_URL", legacy.url.render_as_string(hide_password=False))
    assert acli.main(argv) == 0
    names = sorted(p.name for p in out.iterdir())
    assert names == [
        "gl-attribution-1410-NGN.json",
        "gl-attribution-2000-NGN.json",
        "gl-attribution-2040-NGN.json",
        "gl-attribution-manifest.json",
    ]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in out.iterdir())
    again = tmp_path / "cli-out2"
    again.mkdir()
    # a snapshot sha that differs from the manifest's refuses
    argv[argv.index("--out-dir") + 1] = str(again)
    argv[argv.index("--snapshot-sha256") + 1] = "6" * 64
    monkeypatch.setenv("MIGRATION_DATABASE_URL", dest_url)
    monkeypatch.setenv("LEGACY_DATABASE_URL", legacy.url.render_as_string(hide_password=False))
    assert acli.main(argv) == 2 and list(again.iterdir()) == []
    assert isinstance(UUID(str(env.tenant_id)), UUID) and Path(out).is_dir()
