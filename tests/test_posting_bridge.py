"""Bridge behaviour against a scripted session and recorded accounting commands.

Covers every refusal path, replay/idempotency and savepoint rollback. Postgres
qualification (real unique source identity, RLS, period rows) is hosted CI only.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from dotmac_accounting.contracts import (
    AccountKind,
    Conflict,
    InvalidJournal,
    InvalidPeriod,
    JournalStatus,
    PeriodStatus,
)
from dotmac_accounting.models import Account, FiscalPeriod
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from sqlalchemy.exc import IntegrityError

from erp_v2.posting import bridge, rules
from erp_v2.posting.rules import PostingRefused

TENANT = uuid4()
A1, A2, A3 = (UUID(int=n) for n in (1, 2, 3))
FP = "b" * 64
NOW = datetime(2026, 1, 6, tzinfo=timezone.utc)


def account(uid, code="5000", **over):
    base = dict(id=uid, code=code, is_active=True, posting_allowed=True, kind=AccountKind.POSTING)
    base.update(over)
    return SimpleNamespace(**base)


def period(status=PeriodStatus.OPEN, adjustment=False):
    return SimpleNamespace(status=status, is_adjustment=adjustment)


class FakeDB:
    def __init__(self, accounts, periods=None, existing=None):
        self.accounts, self.existing = list(accounts), existing
        self.periods = [period()] if periods is None else list(periods)
        self.savepoints = []

    @contextmanager
    def begin_nested(self):
        record = {"rolled_back": False}
        self.savepoints.append(record)
        try:
            yield
        except BaseException:
            record["rolled_back"] = True
            raise

    def scalar(self, stmt):
        return self.existing

    def scalars(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Account:
            wanted = stmt.compile().params
            values = {v for v in wanted.values() if isinstance(v, (UUID, str))}
            return [a for a in self.accounts if a.id in values or a.code in values]
        assert entity is FiscalPeriod
        return self.periods


class Accounting:
    def __init__(self, monkeypatch, *, status=JournalStatus.DRAFT, create_error=None, post_error=None):
        self.calls = []
        self.create_error, self.post_error = create_error, post_error
        self.created_status = status
        monkeypatch.setattr(bridge.accounting, "create_journal", self.create)
        monkeypatch.setattr(bridge.accounting, "post_journal", self.post)

    def create(self, db, **kw):
        self.calls.append(("create", kw))
        if self.create_error:
            raise self.create_error
        return SimpleNamespace(id=uuid4(), number=kw["command"].number, status=self.created_status)

    def post(self, db, **kw):
        self.calls.append(("post", kw))
        if self.post_error:
            raise self.post_error
        return SimpleNamespace(id=kw["journal_id"], number="N", status=JournalStatus.POSTED)

    @property
    def names(self):
        return [name for name, _ in self.calls]


def entry(ref, debit="0", credit="0"):
    return SimpleNamespace(
        account_ref=str(ref), debit=Decimal(debit), credit=Decimal(credit), description="d", dimension_refs=()
    )


def consequence(entries=None, owner="payables", kind="supplier_invoice"):
    return SimpleNamespace(
        source=SimpleNamespace(owner=owner, document_kind=kind, document_id="inv-1", version="1", fingerprint=FP),
        posting_date=date(2026, 1, 5),
        description="Supplier invoice",
        currency_code="NGN",
        exchange_rate=Decimal("1"),
        entries=entries or (entry(A1, debit="10"), entry(A2, credit="10")),
        fingerprint=FP,
    )


def run(db, c=None, **over):
    args = dict(scope=TenantScope(TENANT), consequence=c or consequence(), approval_ref="appr-1", actor="p-1", now=NOW)
    args.update(over)
    return bridge.post_consequence(db, **args)


def two_accounts():
    return [account(A1), account(A2, "2000")]


def test_posts_balanced_journal_with_provenance(monkeypatch):
    acc = Accounting(monkeypatch)
    result = run(FakeDB(two_accounts()))
    assert result.replayed is False and acc.names == ["create", "post"]
    create, post = acc.calls[0][1], acc.calls[1][1]
    command = create["command"]
    assert (command.source.owner, command.source.document_kind, command.source.document_id) == (
        "payables",
        "supplier_invoice",
        "inv-1",
    )
    assert command.source.fingerprint == FP and command.reference == "payables:supplier_invoice:inv-1:1"
    assert create["idempotency_key"] == "erp.post:payables:supplier_invoice:inv-1:1:create"
    assert post["idempotency_key"] == "erp.post:payables:supplier_invoice:inv-1:1:post"
    assert post["approval_reference"] == "appr-1" and post["posted_by"] == "p-1"
    assert sum(line.debit for line in command.lines) == sum(line.credit for line in command.lines) == Decimal(10)
    assert {line.account_id for line in command.lines} == {A1, A2}


def test_replay_returns_existing_without_new_writes_even_if_period_now_closed(monkeypatch):
    acc = Accounting(monkeypatch, status=JournalStatus.POSTED)
    db = FakeDB(
        two_accounts(), periods=(period(PeriodStatus.LOCKED),), existing=SimpleNamespace(status=JournalStatus.POSTED)
    )
    result = run(db)
    assert result.replayed is True and acc.names == ["create"]  # create verifies the request fingerprint only


def test_replay_with_different_request_refuses(monkeypatch):
    Accounting(monkeypatch, status=JournalStatus.POSTED, create_error=Conflict("different journal request"))
    db = FakeDB(two_accounts(), existing=SimpleNamespace(status=JournalStatus.POSTED))
    with pytest.raises(PostingRefused) as exc:
        run(db)
    assert exc.value.code == "accounting_refused" and db.savepoints[0]["rolled_back"]


def test_idempotency_key_conflict_refuses(monkeypatch):
    Accounting(monkeypatch, create_error=IdempotencyConflict("scope", "key"))
    with pytest.raises(PostingRefused) as exc:
        run(FakeDB(two_accounts()))
    assert exc.value.code == "accounting_refused"


def test_existing_draft_is_posted_not_duplicated(monkeypatch):
    acc = Accounting(monkeypatch)
    run(FakeDB(two_accounts(), existing=SimpleNamespace(status=JournalStatus.DRAFT)))
    assert acc.names == ["create", "post"]


@pytest.mark.parametrize("state", [JournalStatus.REVERSED, JournalStatus.VOID])
def test_existing_reversed_or_void_refuses(monkeypatch, state):
    acc = Accounting(monkeypatch)
    with pytest.raises(PostingRefused) as exc:
        run(FakeDB(two_accounts(), existing=SimpleNamespace(status=state)))
    assert exc.value.code == "journal_state_refused" and acc.calls == []


def test_unbalanced_refuses_before_any_write(monkeypatch):
    acc = Accounting(monkeypatch)
    db = FakeDB(two_accounts())
    with pytest.raises(PostingRefused) as exc:
        run(db, consequence((entry(A1, debit="10"), entry(A2, credit="9"))))
    assert exc.value.code == "unbalanced" and acc.calls == [] and db.savepoints == []


def test_unknown_account_refuses_and_rolls_back(monkeypatch):
    acc = Accounting(monkeypatch)
    db = FakeDB([account(A1)])
    with pytest.raises(PostingRefused) as exc:
        run(db)
    assert exc.value.code == "account_unknown" and acc.calls == [] and db.savepoints[0]["rolled_back"]


@pytest.mark.parametrize(
    "over",
    [{"is_active": False}, {"posting_allowed": False}, {"kind": AccountKind.CONTROL}],
)
def test_inactive_or_non_postable_account_refuses(monkeypatch, over):
    acc = Accounting(monkeypatch)
    db = FakeDB([account(A1, **over), account(A2, "2000")])
    with pytest.raises(PostingRefused) as exc:
        run(db)
    assert exc.value.code == "account_not_postable" and acc.calls == []


@pytest.mark.parametrize(
    "periods,code",
    [
        ((), "period_missing"),
        ((period(), period()), "period_missing"),
        ((period(PeriodStatus.SOFT_CLOSED),), "period_closed"),
        ((period(PeriodStatus.LOCKED),), "period_closed"),
        ((period(PeriodStatus.REOPENED),), "period_closed"),
        ((period(PeriodStatus.FUTURE),), "period_closed"),
        ((period(PeriodStatus.OPEN, adjustment=True),), "period_closed"),
    ],
)
def test_period_refusals_write_nothing(monkeypatch, periods, code):
    acc = Accounting(monkeypatch)
    db = FakeDB(two_accounts(), periods=periods)
    with pytest.raises(PostingRefused) as exc:
        run(db)
    assert exc.value.code == code and acc.calls == [] and db.savepoints[0]["rolled_back"]


def test_account_ref_forms_mixed_payroll_code_resolves(monkeypatch):
    acc = Accounting(monkeypatch)
    net = account(A3, "2040")
    c = consequence((entry(A1, debit="10"), entry("2040", credit="10")), owner="payroll", kind="payroll_run")
    run(FakeDB([account(A1), net]), c)
    assert {line.account_id for line in acc.calls[0][1]["command"].lines} == {A1, A3}


def test_undeclared_code_form_refuses(monkeypatch):
    acc = Accounting(monkeypatch)
    c = consequence((entry(A1, debit="10"), entry("2040", credit="10")))  # payables declares UUID only
    with pytest.raises(PostingRefused) as exc:
        run(FakeDB(two_accounts()), c)
    assert exc.value.code == "account_ref_form_refused" and acc.calls == []


def test_ambiguous_code_resolution_refuses(monkeypatch):
    Accounting(monkeypatch)
    c = consequence((entry(A1, debit="10"), entry("2040", credit="10")), owner="payroll", kind="payroll_run")
    dup = [account(A1), account(A2, "2040"), account(A3, "2040")]
    with pytest.raises(PostingRefused) as exc:
        run(FakeDB(dup), c)
    assert exc.value.code == "account_unknown"


@pytest.mark.parametrize("error", [InvalidJournal("x"), InvalidPeriod("closed")])
def test_accounting_post_refusal_is_mapped_and_rolled_back(monkeypatch, error):
    Accounting(monkeypatch, post_error=error)
    db = FakeDB(two_accounts())
    with pytest.raises(PostingRefused) as exc:
        run(db)
    assert exc.value.code == "accounting_refused" and db.savepoints[0]["rolled_back"]


def test_integrity_error_is_mapped(monkeypatch):
    Accounting(monkeypatch, create_error=IntegrityError("s", {}, Exception("dup")))
    with pytest.raises(PostingRefused) as exc:
        run(FakeDB(two_accounts()))
    assert exc.value.code == "accounting_conflict"


@pytest.mark.parametrize("over", [{"approval_ref": " "}, {"actor": ""}])
def test_approval_and_actor_are_required(monkeypatch, over):
    acc = Accounting(monkeypatch)
    with pytest.raises(PostingRefused):
        run(FakeDB(two_accounts()), **over)
    assert acc.calls == []


def test_journal_number_is_deterministic_for_replay():
    c = consequence()
    assert rules.journal_number(c.source) == rules.journal_number(consequence().source)
