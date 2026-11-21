"""The live billing -> GL mapper: page ERP's own billing accounting facts and post each exactly once.

Every fact goes through ``posting.bridge.post_consequence`` (owner ``billing``, kind ``accounting_fact``,
document_id = fact id, version = fact version, fingerprint = the fact digest). The caller owns the transaction; this
module commits only through the ``commit`` callable it is given (a dry run passes a no-op and rolls back).

Order and blocking (B15.3): facts are processed in the billing index order; a BLOCK applies per billing account (its
later facts WAIT), other accounts continue; each run retries pending facts first, so a configuration fix heals them
with no staff action. Windows are UTC days up to now - settle; a window closes only when the billing window summary
(count + chained digest) equals the mapper's running ``fact_index_line`` digest; recent closed windows are
re-summarised every run and a mismatch re-pages (idempotent).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from dotmac_accounting.contracts import PeriodStatus
from dotmac_accounting.models import Account, FiscalPeriod
from dotmac_billing import (
    AccountingFactIndexEntryV1,
    ListAccountingFactsV1,
    SummarizeAccountingFactWindowV1,
    get_accounting_fact_v1,
    get_accounting_fact_v2,
    get_accounting_fact_v3,
    get_accounting_fact_v4,
    get_accounting_fact_v5,
    get_opening_window_seal,
    list_accounting_facts,
    summarize_accounting_fact_window,
)
from dotmac_billing.document_queries import fact_index_digest_seed, fact_index_line
from dotmac_billing.models import AllocationEffect, ConfirmedSettlement
from dotmac_kernel.cache import TenantScope
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.billing_gl import rules, settings, vat_plan
from erp_v2.billing_gl.models import (
    BLOCKED,
    NO_GL,
    PENDING,
    POSTED,
    REFUSED,
    SKIP,
    TERMINAL,
    WAITING,
    FactPosting,
    FactWindow,
)
from erp_v2.billing_gl.rules import AllocRow, Disp, Effect, FactView
from erp_v2.journals.policy import FunctionalCurrencyError, functional_currency
from erp_v2.posting import bridge
from erp_v2.posting.rules import PostingRefused

AR_SOURCE: Final = "erp_v2.ar"
OWNER: Final = "billing"
KIND: Final = "accounting_fact"
ACTOR: Final = "erp.billing_gl"
PAGE: Final = 500
ONE_US: Final = timedelta(microseconds=1)
FLOOR_DAY_MIN: Final = date(1970, 1, 1)


class BillingFactDigestChanged(RuntimeError):
    """Integrity alarm: a fact already dispositioned came back with a different digest."""


@dataclass(slots=True)
class Report:
    tenant_enabled: bool = False
    posted: int = 0
    would_post: int = 0
    skipped: int = 0
    no_gl: int = 0
    refused: int = 0
    blocked: int = 0
    waiting: int = 0
    windows_closed: int = 0
    windows_mismatched: int = 0
    windows_remaining: int = 0
    locked: bool = False
    integrity_alarms: int = 0
    # Counts only: labels carry reasons, never identifiers or amounts.
    blocked_by_reason: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


@dataclass(frozen=True, slots=True)
class _Source:
    owner: str
    document_kind: str
    document_id: str
    version: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _Entry:
    account_ref: str
    debit: Decimal
    credit: Decimal
    description: str | None
    dimension_refs: tuple = ()


@dataclass(frozen=True, slots=True)
class _Consequence:
    source: _Source
    posting_date: date
    description: str
    currency_code: str
    exchange_rate: Decimal
    entries: tuple[_Entry, ...]
    fingerprint: str


def _fingerprint(source: _Source, posting_date: date, currency: str, lines) -> str:
    body = {
        "source": [source.owner, source.document_kind, source.document_id, source.version, source.fingerprint],
        "date": posting_date.isoformat(),
        "currency": currency,
        "lines": [[ln.account_ref, format(ln.debit, "f"), format(ln.credit, "f")] for ln in lines],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def utc_day(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def v4_alloc_rows(fact) -> tuple[AllocRow, ...]:
    """Every settlement of a document by a V4 fact: settlement allocations AND opening-lot applications.

    An advance drawn from an opening cash lot sits in ``opening_cash_effects`` (application / application_inverse),
    not in ``settlement_allocations``; folding only the latter would leave such an application's VAT on 1455 forever.
    """
    rows = [
        AllocRow(
            a.allocation_effect_id,
            a.allocation.document_id,
            a.allocation.amount_delta.amount,
            a.allocation.offsets_allocation_id,
        )
        for a in fact.settlement_allocations
    ]
    for effect in fact.opening_cash_effects:
        if effect.effect_kind == "application":
            rows.append(AllocRow(effect.effect_id, effect.document_id, effect.amount.amount, None))
        elif effect.effect_kind == "application_inverse":
            rows.append(
                AllocRow(effect.effect_id, effect.document_id, -effect.amount.amount, effect.original_effect_id)
            )
    return tuple(rows)


class Mapper:
    def __init__(
        self,
        db: Session,
        tenant_id: UUID,
        *,
        now: datetime,
        commit: Callable[[], None],
        enable_once: bool = False,
        max_windows: int = 1000,
    ) -> None:
        self.db, self.tenant_id, self.now = db, tenant_id, now
        self.scope = TenantScope(tenant_id)
        self.commit = commit
        self.settings = settings.load(db, tenant_id)
        self.live = self.settings.enabled
        self.may_post = self.live  # --enable-once dispositions facts but never posts
        self.enable_once = enable_once
        self.max_windows = max_windows
        self.report = Report(tenant_enabled=self.live)
        self.blocked_accounts: set[UUID] = set()
        try:
            self.functional: str | None = functional_currency(db, tenant_id)
        except FunctionalCurrencyError:
            self.functional = None
        seal = get_opening_window_seal(db, scope=self.scope)
        self.seal_at = None if seal is None else seal.recorded_at
        found = list(db.scalars(select(Account.id).where(Account.tenant_id == tenant_id, Account.code == "1400")))
        self.receivable = str(found[0]) if len(found) == 1 else None
        self.map_digest = self.settings.map_digest

    # ------------------------------------------------------------------ rows
    def _row(self, fact_id: UUID, version: int) -> FactPosting | None:
        return self.db.scalar(
            select(FactPosting).where(
                FactPosting.tenant_id == self.tenant_id,
                FactPosting.fact_id == fact_id,
                FactPosting.fact_version == version,
            )
        )

    def _record(
        self,
        entry: AccountingFactIndexEntryV1,
        currency: str | None,
        disposition: str,
        reason: str | None,
        journal_id: UUID | None = None,
    ) -> None:
        cursor = entry.cursor
        row = self._row(cursor.fact_id, cursor.fact_version)
        moment = self.now
        if row is None:
            self.db.add(
                FactPosting(
                    tenant_id=self.tenant_id,
                    fact_id=cursor.fact_id,
                    fact_version=cursor.fact_version,
                    effect_kind=entry.effect_kind,
                    fact_digest=entry.fact_digest,
                    billing_account_id=cursor.billing_account_id,
                    currency=currency,
                    occurred_at=cursor.occurred_at,
                    disposition=disposition,
                    reason=reason[:80] if reason else None,
                    journal_id=journal_id,
                    map_digest=self.map_digest if disposition == POSTED else None,
                    attempts=1,
                    first_seen_at=moment,
                    updated_at=moment,
                )
            )
        else:
            if row.disposition == disposition and row.reason == (reason[:80] if reason else None):
                return  # unchanged pending state: no write churn on every scheduled run
            row.disposition = disposition
            row.reason = reason[:80] if reason else None
            row.journal_id = journal_id
            row.map_digest = self.map_digest if disposition == POSTED else row.map_digest
            row.attempts = row.attempts + 1
            row.updated_at = moment
        self.db.flush()
        counts = self.report
        if disposition == POSTED:
            counts.posted += 1
        elif disposition == SKIP:
            counts.skipped += 1
        elif disposition == NO_GL:
            counts.no_gl += 1
        elif disposition == REFUSED:
            counts.refused += 1

    # ------------------------------------------------------------ fact reads
    def _view(self, entry: AccountingFactIndexEntryV1, rule: rules.Rule) -> FactView:
        cursor = entry.cursor
        base = {
            "version": cursor.fact_version,
            "kind": entry.effect_kind,
            "fact_id": cursor.fact_id,
            "billing_account_id": cursor.billing_account_id,
            "occurred_at": cursor.occurred_at,
        }
        if not rule.needs_fact:
            return FactView(currency=None, **base)
        version = cursor.fact_version
        if version == 1:
            fact = get_accounting_fact_v1(self.db, scope=self.scope, fact_id=cursor.fact_id)
            moneys = [e.amount_delta for e in fact.effects] + [a.amount_delta for a in fact.allocations]
            return FactView(
                minor_units=moneys[0].currency.minor_units if moneys else 2,
                alloc_rows=self._alloc_rows(entry) if rule.vat else (),
                tax_snapshots=tuple((t.treatment_code, t.tax_amount.amount) for t in fact.tax_snapshots),
                currency=moneys[0].currency.code if moneys else None,
                committed_at=fact.committed_at,
                effects=tuple(Effect(e.lane, e.amount_delta.amount) for e in fact.effects),
                settlement_ids=tuple(dict.fromkeys(a.settlement_id for a in fact.allocations)),
                reverses_fact_id=fact.reverses_fact_id,
                source_ref=fact.source_ref,
                **base,
            )
        if version == 2:
            v2 = get_accounting_fact_v2(self.db, scope=self.scope, fact_id=cursor.fact_id)
            moneys2 = [e.amount_delta for e in v2.effects]
            return FactView(
                currency=moneys2[0].currency.code if moneys2 else None,
                committed_at=v2.committed_at,
                effects=tuple(Effect(e.lane, e.amount_delta.amount) for e in v2.effects),
                settlement_ids=tuple(
                    dict.fromkeys(d.settlement_id for d in v2.withholding_discharges if d.settlement_id is not None)
                ),
                reverses_fact_id=v2.reverses_fact_id,
                source_ref=v2.source_ref,
                **base,
            )
        if version == 3:
            v3 = get_accounting_fact_v3(self.db, scope=self.scope, fact_id=cursor.fact_id)
            moneys3 = [e.amount_delta for e in v3.effects]
            return FactView(
                currency=moneys3[0].currency.code if moneys3 else None,
                committed_at=v3.committed_at,
                effects=tuple(Effect(e.lane, e.amount_delta.amount) for e in v3.effects),
                source_ref=v3.source_ref,
                **base,
            )
        if version == 4:
            v4 = get_accounting_fact_v4(self.db, scope=self.scope, posting_group_id=entry.posting_group_id)
            moneys4 = [e.amount_delta for e in v4.effects]
            return FactView(
                minor_units=moneys4[0].currency.minor_units if moneys4 else 2,
                alloc_rows=v4_alloc_rows(v4),
                currency=moneys4[0].currency.code if moneys4 else None,
                committed_at=v4.committed_at,
                effects=tuple(Effect(e.lane, e.amount_delta.amount) for e in v4.effects),
                source_ref=v4.source_ref,
                **base,
            )
        if version == 5:
            v5 = get_accounting_fact_v5(self.db, scope=self.scope, posting_group_id=entry.posting_group_id)
            return FactView(currency=None, committed_at=v5.committed_at, source_ref=v5.source_ref, **base)
        raise RuntimeError("fact_contract_unreadable")

    def _alloc_rows(self, entry: AccountingFactIndexEntryV1) -> tuple[AllocRow, ...]:
        """The persisted allocation effects of the fact's posting group (ids are needed to pair a deallocation)."""
        rows = self.db.scalars(
            select(AllocationEffect).where(
                AllocationEffect.tenant_id == self.tenant_id,
                AllocationEffect.posting_group_id == entry.posting_group_id,
            )
        )
        return tuple(
            AllocRow(r.id, r.document_id, Decimal(r.amount_delta), r.offsets_allocation_id)
            for r in sorted(rows, key=lambda r: str(r.id))
        )

    def _receipt_cash_account(self, settlement_ids: tuple[UUID, ...]) -> set[str]:
        """B-16 ``erp_ar_receipts.cash_account_id`` of the settlements (a structural join on the settlement id)."""
        if not settlement_ids or not self.db.scalar(
            sa.text("SELECT to_regclass('public.erp_ar_receipts') IS NOT NULL")
        ):
            return set()
        found = self.db.execute(
            sa.text(
                "SELECT cash_account_id FROM erp_ar_receipts WHERE tenant_id = :t AND billing_settlement_id = ANY(:ids)"
            ),
            {"t": self.tenant_id, "ids": list(settlement_ids)},
        )
        return {str(row[0]) for row in found}

    def _settlement_source(self, settlement_id: UUID) -> str | None:
        """The settlement's source system: a read-only ORM read (no public getter without a billing-side write)."""
        return self.db.scalar(
            select(ConfirmedSettlement.source_system).where(
                ConfirmedSettlement.tenant_id == self.tenant_id, ConfirmedSettlement.id == settlement_id
            )
        )

    def _settlement_ids_for(self, view: FactView, rule: rules.Rule) -> tuple[UUID, ...]:
        if view.settlement_ids:
            return view.settlement_ids
        if view.kind == "settlement_accepted" and view.source_ref.startswith("settlement:"):
            try:
                return (UUID(view.source_ref.removeprefix("settlement:")),)
            except ValueError:
                return ()
        if rule.cash == rules.CASH_REVERSAL and view.reverses_fact_id is not None:
            original = get_accounting_fact_v1(self.db, scope=self.scope, fact_id=view.reverses_fact_id)
            ids = tuple(dict.fromkeys(a.settlement_id for a in original.allocations))
            if ids:
                return ids
            if original.source_ref.startswith("settlement:"):
                try:
                    return (UUID(original.source_ref.removeprefix("settlement:")),)
                except ValueError:
                    return ()
        return ()

    # ---------------------------------------------------------- posting date
    def _posting_date(self, occurred_at: datetime) -> tuple[date, date | None] | str:
        tz = ZoneInfo(self.settings.posting_timezone)
        day = occurred_at.astimezone(tz).date()
        periods = list(
            self.db.scalars(
                select(FiscalPeriod)
                .where(FiscalPeriod.tenant_id == self.tenant_id, FiscalPeriod.is_adjustment.is_(False))
                .order_by(FiscalPeriod.start_date)
            )
        )
        containing = [p for p in periods if p.start_date <= day <= p.end_date]
        if len(containing) == 1 and containing[0].status == PeriodStatus.OPEN:
            return day, None
        if self.settings.closed_period_policy == settings.HOLD:
            return "period_closed" if containing else "period_missing"
        later = [p for p in periods if p.status == PeriodStatus.OPEN and p.end_date >= day]
        if not later:
            return "period_closed" if containing else "period_missing"
        target = max(day, later[0].start_date)
        return target, day

    # --------------------------------------------------------------- process
    def _plan(self, rule: rules.Rule, view: FactView, entry, reversed_kind, pred) -> rules.Plan | None:
        """The VAT / document plan of a fact (rev 2); None for kinds without one."""
        if not rule.vat:
            return None
        if view.kind == "invoice_issued":
            if self.receivable is None:
                return rules.Plan(block="receivable_account_unmapped")
            return vat_plan.issue_plan(self.db, self.scope, view, self.receivable, self.settings.tax_accounts)
        if view.kind == "credit_note_issued":
            if self.receivable is None:
                return rules.Plan(block="receivable_account_unmapped")
            return vat_plan.credit_plan(self.db, self.scope, view, self.receivable, self.settings.tax_accounts)
        if view.kind == "reversal":
            if view.reverses_fact_id is None:
                return rules.Plan(block="waiting_predecessor")
            return vat_plan.reversal_plan(
                self.db, self.scope, view, reversed_kind, None if pred is None else pred.journal_id
            )
        return vat_plan.allocation_plan(self.db, self.scope, view)

    def _env(self, view: FactView, rule: rules.Rule, entry=None, pred=None) -> rules.Env:
        sources: tuple[str, ...] = ()
        reversed_kind = None
        cash_accounts = dict(self.settings.cash_accounts)
        cash_block = None
        if rule.cash in (rules.CASH_SETTLEMENT, rules.CASH_REVERSAL):
            ids = self._settlement_ids_for(view, rule)
            found = [self._settlement_source(i) for i in ids]
            sources = tuple(s for s in found if s)
            if AR_SOURCE in sources:
                # The bank of an ERP receipt is STRUCTURE (its erp_ar_receipts row), never a typed setting: exactly
                # one receipt row, else the fact blocks. There is no fallback to billing_gl.cash_account.
                receipt_accounts = self._receipt_cash_account(ids)
                cash_accounts = {k: v for k, v in cash_accounts.items() if k != AR_SOURCE}
                if len(receipt_accounts) == 1:
                    cash_accounts[AR_SOURCE] = next(iter(receipt_accounts))
                else:
                    cash_block = "receipt_cash_unresolved"
        if rule.cash == rules.CASH_REVERSAL and view.reverses_fact_id is not None:
            original = self._row(view.reverses_fact_id, 1)
            reversed_kind = None if original is None else original.effect_kind
        return rules.Env(
            functional_currency=self.functional,
            receivable_account=self.receivable,
            prepayment_account=self.settings.prepayment_account,
            prepaid_funding_account=self.settings.prepaid_funding_account,
            cash_accounts=cash_accounts,
            cash_block=cash_block,
            seal_at=self.seal_at,
            settlement_sources=sources,
            reversed_kind=reversed_kind,
            plan=self._plan(rule, view, entry, reversed_kind, pred),
        )

    def process(self, entry: AccountingFactIndexEntryV1) -> str:
        """Disposition one fact. Returns the disposition name (or ``WOULD_POST`` when posting is idle)."""
        cursor = entry.cursor
        row = self._row(cursor.fact_id, cursor.fact_version)
        if row is not None:
            if row.fact_digest != entry.fact_digest:
                self.report.integrity_alarms += 1
                raise BillingFactDigestChanged("billing_fact_digest_changed")
            if row.disposition in TERMINAL:
                return row.disposition
        account = cursor.billing_account_id
        rule = rules.rule_for(cursor.fact_version, entry.effect_kind)
        if row is not None and row.disposition == REFUSED and rule.action == rules.REFUSE:
            return REFUSED
        if rule.action != rules.REFUSE and account in self.blocked_accounts:
            if row is None or row.disposition != WAITING or row.reason != "waiting_predecessor":
                self._record(entry, row.currency if row else None, WAITING, "waiting_predecessor")
            return WAITING
        try:
            with self.db.begin_nested():
                view = self._view(entry, rule)
                pred = None
                if rule.predecessor and view.reverses_fact_id is not None:
                    pred = self._row(view.reverses_fact_id, 1)
                    if pred is None or pred.disposition not in (POSTED, NO_GL):
                        decision = rules.Decision(Disp.BLOCKED, "waiting_predecessor")
                        return self._finish(entry, view, decision, waiting=True)
                decision = rules.decide(rule, view, self._env(view, rule, entry, pred))
        except BillingFactDigestChanged:
            raise
        except Exception as exc:  # a billing read refusal is a BLOCK that retries, never a crash
            self._record(entry, None, BLOCKED, f"fact_read_failed:{type(exc).__name__}")
            self.blocked_accounts.add(account)
            return BLOCKED
        return self._finish(entry, view, decision)

    def _finish(
        self, entry: AccountingFactIndexEntryV1, view: FactView, decision: rules.Decision, waiting: bool = False
    ) -> str:
        account = entry.cursor.billing_account_id
        if decision.disposition == Disp.POST:
            return self._post(entry, view, decision)
        if decision.disposition == Disp.BLOCKED:
            # waiting_document: this fact sorted before its own invoice's issue fact. It waits for that fact but does
            # NOT block the account (the invoice must post), and the next run's retry re-evaluates it.
            ordering = decision.reason == "waiting_document"
            state = WAITING if (waiting or ordering) else BLOCKED
            self._record(entry, view.currency, state, decision.reason)
            if not ordering:
                self.blocked_accounts.add(account)
            return state
        self._record(entry, view.currency, decision.disposition.value, decision.reason)
        return decision.disposition.value

    def _post(self, entry: AccountingFactIndexEntryV1, view: FactView, decision: rules.Decision) -> str:
        account = entry.cursor.billing_account_id
        if not self.may_post:
            self.report.would_post += 1
            return "WOULD_POST"
        resolved = self._posting_date(view.occurred_at)
        if isinstance(resolved, str):
            self._record(entry, view.currency, BLOCKED, resolved)
            self.blocked_accounts.add(account)
            return BLOCKED
        posting_date, original = resolved
        source = _Source(OWNER, KIND, str(view.fact_id), str(entry.cursor.fact_version), entry.fact_digest)
        text = f"Billing {view.kind} {view.fact_id}"
        if original is not None:
            text += f" (original date {original.isoformat()}; posted to the next open period)"
        consequence = _Consequence(
            source=source,
            posting_date=posting_date,
            description=text,
            currency_code=view.currency or "",
            exchange_rate=Decimal(1),
            entries=tuple(_Entry(ln.account_ref, ln.debit, ln.credit, ln.description) for ln in decision.lines),
            fingerprint=_fingerprint(source, posting_date, view.currency or "", decision.lines),
        )
        try:
            with self.db.begin_nested():
                result = bridge.post_consequence(
                    self.db,
                    scope=self.scope,
                    consequence=consequence,
                    approval_ref=f"billing_gl:{self.map_digest}",
                    actor=ACTOR,
                    now=self.now,
                )
                if decision.persist is not None:
                    vat_plan.persist(
                        self.db,
                        self.tenant_id,
                        view.fact_id,
                        entry.cursor.fact_version,
                        result.journal_id,
                        decision.persist,
                    )
        except PostingRefused as exc:
            self._record(entry, view.currency, BLOCKED, exc.code)
            self.blocked_accounts.add(account)
            return BLOCKED
        except IntegrityError as exc:
            # A database refusal: the journal rolled back with the savepoint, so this ONE fact blocks instead of
            # aborting the run and stalling every account. Only the VAT trigger's own message is a vat_invariant.
            reason = "vat_invariant" if "vat_invariant" in str(getattr(exc, "orig", exc)) else "integrity_refused"
            self._record(entry, view.currency, BLOCKED, reason)
            self.blocked_accounts.add(account)
            return BLOCKED
        self._record(entry, view.currency, POSTED, None, result.journal_id)
        return POSTED

    # ------------------------------------------------------------- the loop
    def _retry_pending(self) -> None:
        """Retry every BLOCKED/WAITING fact, in billing index order, paging EVERY slice to the end.

        Opening facts can share one timestamp (#72 stamps them with a single ``--occurred-at``), so a slice can hold
        more than one page; a pending fact is matched in whichever page it is on, never dropped after the first.
        """
        pending = {
            (row.fact_id, row.fact_version): row.occurred_at
            for row in self.db.scalars(
                select(FactPosting).where(FactPosting.tenant_id == self.tenant_id, FactPosting.disposition.in_(PENDING))
            )
        }
        for stamp in sorted(set(pending.values())):
            wanted = {key for key, at in pending.items() if at == stamp}
            after = None
            while wanted:
                page = list_accounting_facts(
                    self.db,
                    scope=self.scope,
                    request=ListAccountingFactsV1(self.scope, stamp, stamp + ONE_US, after, PAGE),
                )
                for entry in page.entries:
                    key = (entry.cursor.fact_id, entry.cursor.fact_version)
                    if key in wanted:
                        wanted.discard(key)
                        self.process(entry)
                if page.next_cursor is None:
                    break
                after = page.next_cursor

    def _page_window(self, start: datetime, end: datetime) -> tuple[int, str]:
        digest = fact_index_digest_seed()
        count = 0
        after = None
        while True:
            page = list_accounting_facts(
                self.db,
                scope=self.scope,
                request=ListAccountingFactsV1(self.scope, start, end, after, PAGE),
            )
            for entry in page.entries:
                self.process(entry)
                cursor = entry.cursor
                digest.update(fact_index_line(cursor.fact_id, cursor.fact_version, entry.fact_digest))
                count += 1
            self.commit()
            if page.next_cursor is None:
                return count, digest.hexdigest()
            after = page.next_cursor

    def _sync_window(self, day: date, existing: FactWindow | None, *, recheck: bool) -> None:
        start, end = utc_day(day), utc_day(day + timedelta(days=1))
        if existing is not None and existing.closed_at is not None and recheck:
            summary = summarize_accounting_fact_window(
                self.db, scope=self.scope, request=SummarizeAccountingFactWindowV1(self.scope, start, end)
            )
            if summary.count == existing.count and summary.digest == existing.digest:
                existing.rechecked_at = self.now
                self.commit()
                return
            self.report.windows_mismatched += 1
            existing.closed_at = None  # a late-committed fact: re-page; idempotent
            self.db.flush()
        idle_before = self.report.would_post
        count, digest = self._page_window(start, end)
        summary = summarize_accounting_fact_window(
            self.db, scope=self.scope, request=SummarizeAccountingFactWindowV1(self.scope, start, end)
        )
        # A window holding a fact the idle mapper could not post is NOT closed: enabling later must revisit it.
        match = summary.count == count and summary.digest == digest and self.report.would_post == idle_before
        if existing is None:
            self.db.add(
                FactWindow(
                    tenant_id=self.tenant_id,
                    occurred_from=start,
                    occurred_to=end,
                    count=count,
                    digest=digest,
                    closed_at=self.now if match else None,
                    rechecked_at=self.now if match else None,
                )
            )
        else:
            existing.count, existing.digest = count, digest
            existing.closed_at = self.now if match else None
            existing.rechecked_at = self.now if match else existing.rechecked_at
        if match:
            self.report.windows_closed += 1
        else:
            self.report.windows_mismatched += 1
        self.db.flush()
        self.commit()

    def run(self) -> Report:
        """One pass, under a per-tenant advisory lock so two overlapping runs can never process the same facts."""
        if not self.live and not self.enable_once:
            return self.report
        bind = self.db.get_bind()
        key = int.from_bytes(hashlib.sha256(f"billing_gl:{self.tenant_id}".encode()).digest()[:8], "big", signed=True)
        # A dedicated connection: a session-level lock must outlive the per-page commits of the working session.
        # AUTOCOMMIT: the lock connection must not sit idle-in-transaction for the whole run, or an
        # idle_in_transaction_session_timeout would kill it and silently free the lock mid-run.
        with bind.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
            if not lock.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar():
                self.report.locked = True
                return self.report
            try:
                return self._run()
            finally:
                lock.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    def _run(self) -> Report:
        self.report.tenant_enabled = self.live
        self._retry_pending()
        self.commit()
        horizon = self.now - timedelta(seconds=self.settings.settle_seconds)
        last_day = horizon.date()  # windows strictly before this UTC day are complete at the horizon
        if utc_day(last_day + timedelta(days=1)) <= horizon:
            last_day = last_day + timedelta(days=1)
        floor = max(self.settings.history_floor, FLOOR_DAY_MIN)
        windows = {
            w.occurred_from: w
            for w in self.db.scalars(select(FactWindow).where(FactWindow.tenant_id == self.tenant_id))
        }
        recheck_from = self.now - timedelta(days=self.settings.resummarize_days)
        todo: list[tuple[date, FactWindow | None, bool]] = []
        day = floor
        while day < last_day:
            existing = windows.get(utc_day(day))
            if existing is None or existing.closed_at is None:
                todo.append((day, existing, False))
            elif utc_day(day) >= recheck_from:
                todo.append((day, existing, True))
            day += timedelta(days=1)
        # A rolling rescan of OLD closed windows: a fact backdated past resummarize_days is still found, on a cycle
        # of rescan_days. ready() and the period guard refuse while any window is staler than that.
        queued = {when for when, _, _ in todo}
        stale = sorted(
            (w for w in windows.values() if w.closed_at is not None and w.occurred_from.date() not in queued),
            key=lambda w: w.rechecked_at or w.closed_at,
        )
        # --enable-once (the mapper idle) rescans EVERY stale window: the rolling batch only runs while live, so the
        # runbook re-runs it just before the gate and the period guard (which bites from F8 on) can be healed.
        batch = stale if not self.live else stale[: self.settings.rescan_batch]
        for window in batch:
            todo.append((window.occurred_from.date(), window, True))
        for index, (when, existing, recheck) in enumerate(todo):
            if index >= self.max_windows:
                self.report.windows_remaining = len(todo) - index
                break
            self._sync_window(when, existing, recheck=recheck)
        self.commit()
        self._tally()
        return self.report

    def _tally(self) -> None:
        counts: Counter[str] = Counter()
        for disposition, reason in self.db.execute(
            select(FactPosting.disposition, FactPosting.reason).where(
                FactPosting.tenant_id == self.tenant_id, FactPosting.disposition.in_((*PENDING, REFUSED))
            )
        ):
            counts[f"{disposition}:{reason}"] += 1
        self.report.blocked_by_reason = dict(counts)
        self.report.blocked = sum(v for k, v in counts.items() if k.startswith("BLOCKED"))
        self.report.waiting = sum(v for k, v in counts.items() if k.startswith("WAITING"))
