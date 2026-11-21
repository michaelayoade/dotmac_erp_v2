"""Pure dispatch for the live billing -> GL mapper: which accounting facts post, skip, refuse or block, and how.

No database, no billing import. The table is a code constant keyed by (fact_version, effect_kind); an unknown pair
blocks (``unknown_fact_kind``) so a future fact kind can never be silently dropped. To add a mapping (rev 2:
``invoice_issued`` / ``credit_note_issued``) replace one DISPATCH entry and, if it needs new line building, add a
builder to ``BUILDERS``; ``decide`` does not change.

Lane rule L, one line per effect (delta in major units):
  receivable        delta > 0 -> Dr 1400,                 delta < 0 -> Cr 1400
  available_credit  delta > 0 -> Cr prepayment liability, delta < 0 -> Dr prepayment liability
  prepaid_funding   as available_credit on ``ar.prepaid_funding_account`` (T6: unset -> BLOCK)
A lane-only kind must balance. A cash kind carries one residual line on the settlement's source-system cash account.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID


class Disp(StrEnum):
    POST = "POST"
    SKIP = "SKIP"
    NO_GL = "NO_GL"
    REFUSED = "REFUSED"
    BLOCKED = "BLOCKED"


# Rule actions.
POST: Final = "post"
SKIP_OPENING: Final = "skip_opening"
SKIP_UNWIND: Final = "skip_unwind"
REFUSE: Final = "refuse"
BLOCK: Final = "block"

# Cash counterpart sources.
CASH_SETTLEMENT: Final = "settlement"
CASH_REVERSAL: Final = "reversal"  # only when the reversed fact is settlement-family
CASH_OPENING_REFUND: Final = "opening_refund"

# Reversed fact kinds that are settlement-family (their reversal moves cash).
SETTLEMENT_FAMILY: Final = frozenset({"settlement_accepted", "refund"})


@dataclass(frozen=True, slots=True)
class Rule:
    action: str
    reason: str | None = None
    cash: str | None = None
    predecessor: bool = False
    # False when the fact body must NOT be read (no public getter, or none needed).
    needs_fact: bool = True


def _post(**kw) -> Rule:
    return Rule(POST, **kw)


DISPATCH: Final = MappingProxyType(
    {
        # V1
        (1, "invoice_issued"): Rule(BLOCK, "invoice_mapping_not_built", needs_fact=False),
        (1, "credit_note_issued"): Rule(BLOCK, "invoice_mapping_not_built", needs_fact=False),
        (1, "settlement_accepted"): _post(cash=CASH_SETTLEMENT),
        (1, "allocation"): _post(),
        (1, "deallocation"): _post(),
        (1, "reallocation"): _post(),
        (1, "refund"): _post(cash=CASH_SETTLEMENT),
        (1, "reversal"): _post(cash=CASH_REVERSAL, predecessor=True),
        # V2 WHT, V3 non-cash: readable through billing a13's public getters (R9), but no GL treatment is defined
        # for them yet: the fact is read (integrity verified) and BLOCKED by name, never dropped.
        (2, "withholding_accepted"): Rule(BLOCK, "wht_mapping_not_built"),
        (2, "withholding_reversed"): Rule(BLOCK, "wht_mapping_not_built"),
        (3, "non_cash_credit_granted"): Rule(BLOCK, "non_cash_credit_mapping_not_built"),
        (3, "non_cash_credit_applied"): Rule(BLOCK, "non_cash_credit_mapping_not_built"),
        (3, "non_cash_credit_application_reversed"): Rule(BLOCK, "non_cash_credit_mapping_not_built"),
        (3, "non_cash_credit_reduced"): Rule(BLOCK, "non_cash_credit_mapping_not_built"),
        # V4 opening cash
        (4, "opening_cash_imported"): Rule(SKIP_OPENING),
        (4, "opening_cash_import_reversed"): Rule(SKIP_UNWIND),
        (4, "opening_cash_applied"): _post(),
        (4, "cash_credit_draw_reversed"): _post(),
        (4, "opening_cash_refunded"): _post(cash=CASH_OPENING_REFUND),
        (4, "opening_cash_refund_corrected"): _post(cash=CASH_OPENING_REFUND),
        # V5 opening account
        (5, "opening_account_imported"): Rule(SKIP_OPENING),
        (5, "opening_account_import_reversed"): Rule(SKIP_UNWIND),
        # V6 opening variance: effect-free ledger-lane facts; never decoded, never move a tie (T3).
        (6, "opening_variance_recorded"): Rule(REFUSE, "v6_ledger_lane_unmapped", needs_fact=False),
        (6, "opening_variance_voided"): Rule(REFUSE, "v6_ledger_lane_unmapped", needs_fact=False),
        (6, "opening_variance_recognised"): Rule(REFUSE, "v6_ledger_lane_unmapped", needs_fact=False),
        (6, "opening_variance_position_corrected"): Rule(REFUSE, "v6_ledger_lane_unmapped", needs_fact=False),
    }
)

UNKNOWN: Final = Rule(BLOCK, "unknown_fact_kind", needs_fact=False)


def rule_for(version: int, kind: str) -> Rule:
    return DISPATCH.get((version, kind), UNKNOWN)


@dataclass(frozen=True, slots=True)
class Effect:
    lane: str
    delta: Decimal  # signed, major units


@dataclass(frozen=True, slots=True)
class FactView:
    """What the decision needs from a decoded fact. No approval fields: V6 is never decoded (T3)."""

    version: int
    kind: str
    fact_id: UUID
    billing_account_id: UUID
    currency: str | None
    occurred_at: datetime
    committed_at: datetime | None = None
    effects: tuple[Effect, ...] = ()
    # Evidence the mapper uses to find the cash account of settlement-family facts.
    settlement_ids: tuple[UUID, ...] = ()
    reverses_fact_id: UUID | None = None
    source_ref: str = ""


@dataclass(frozen=True, slots=True)
class Line:
    account_ref: str
    debit: Decimal
    credit: Decimal
    description: str


@dataclass(frozen=True, slots=True)
class Env:
    """Pre-resolved context so ``decide`` stays pure."""

    functional_currency: str | None
    receivable_account: str | None
    prepayment_account: str | None
    prepaid_funding_account: str | None
    cash_accounts: dict[str, str] = field(default_factory=dict)
    seal_at: datetime | None = None
    # source_system of every settlement the fact touches (resolved by the mapper).
    settlement_sources: tuple[str, ...] = ()
    # Kind of the reversed fact (reversal only), or None.
    reversed_kind: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    disposition: Disp
    reason: str | None = None
    lines: tuple[Line, ...] = ()


def _block(reason: str) -> Decision:
    return Decision(Disp.BLOCKED, reason)


def _lane_line(effect: Effect, env: Env) -> Line | Decision:
    amount = abs(effect.delta)
    if effect.lane == "receivable":
        account, debit = env.receivable_account, effect.delta > 0
        missing = "receivable_account_unmapped"
    elif effect.lane == "available_credit":
        account, debit = env.prepayment_account, effect.delta < 0
        missing = "prepayment_account_unmapped"
    elif effect.lane == "prepaid_funding":
        account, debit = env.prepaid_funding_account, effect.delta < 0
        missing = "prepaid_funding_unmapped"
    else:
        return _block("unknown_lane")
    if account is None:
        return _block(missing)
    return Line(account, amount if debit else Decimal(0), Decimal(0) if debit else amount, f"billing {effect.lane}")


def _cash_source(rule: Rule, env: Env) -> str | Decision | None:
    """The cash-account key this fact needs, None when it needs no cash line, or a BLOCK."""
    if rule.cash is None:
        return None
    if rule.cash == CASH_OPENING_REFUND:
        return "opening_refund"
    if rule.cash == CASH_REVERSAL and env.reversed_kind not in SETTLEMENT_FAMILY:
        return None
    sources = set(env.settlement_sources)
    if not sources:
        return _block("settlement_unresolved")
    if len(sources) > 1:
        return _block("cash_sources_ambiguous")
    return next(iter(sources))


def decide(rule: Rule, view: FactView, env: Env) -> Decision:
    """The disposition of one fact. POST decisions carry the balanced lines; everything else carries a reason."""
    if rule.action == REFUSE:
        return Decision(Disp.REFUSED, rule.reason)
    if rule.action == BLOCK:
        return _block(rule.reason or "blocked")
    if rule.action in (SKIP_OPENING, SKIP_UNWIND):
        if env.seal_at is None or view.committed_at is None:
            return _block("opening_seal_unavailable")
        if view.committed_at <= env.seal_at:
            return Decision(Disp.SKIP, "opening_gl_carried")
        return _block("opening_fact_after_seal" if rule.action == SKIP_OPENING else "opening_unwind_post_seal")
    if rule.action != POST:
        return _block("unknown_fact_kind")
    builder = BUILDERS.get((view.version, view.kind), build_lane_lines)
    return builder(rule, view, env)


def build_lane_lines(rule: Rule, view: FactView, env: Env) -> Decision:
    effects = tuple(e for e in view.effects if e.delta != 0)
    if not effects:
        return Decision(Disp.NO_GL, "lane_neutral")
    if env.functional_currency is None:
        return _block("functional_currency_unset")
    if view.currency != env.functional_currency:
        return _block("billing_fx_unsupported")
    lines: list[Line] = []
    for effect in effects:
        built = _lane_line(effect, env)
        if isinstance(built, Decision):
            return built
        lines.append(built)
    source = _cash_source(rule, env)
    if isinstance(source, Decision):
        return source
    net = sum((ln.debit - ln.credit for ln in lines), Decimal(0))
    if source is not None and net != 0:
        account = env.cash_accounts.get(source)
        if account is None:
            return _block("cash_account_unmapped")
        lines.append(
            Line(
                account,
                -net if net < 0 else Decimal(0),
                net if net > 0 else Decimal(0),
                f"billing cash {source}",
            )
        )
    elif net != 0:
        return _block("lane_effects_unbalanced")
    if len(lines) < 2 or sum((ln.debit - ln.credit for ln in lines), Decimal(0)) != 0:
        return _block("lane_effects_unbalanced")
    return Decision(Disp.POST, None, tuple(lines))


# Per-(version, kind) line builders for kinds that need more than the lane rule (rev 2 invoice kinds).
BUILDERS: Final[dict[tuple[int, str], Callable[[Rule, FactView, Env], Decision]]] = {}
