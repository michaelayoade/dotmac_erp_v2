"""B-15 mapper units: dispatch table, lane math, refusal codes, T2 ties, V6 shapes (N2), settings digest."""

import typing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_billing import contracts as billing_contracts
from dotmac_billing import opening_cash_contracts, opening_receivable_contracts, opening_variance_contracts

from erp_v2.billing_gl import rules
from erp_v2.billing_gl.checks import period_bounds_utc
from erp_v2.billing_gl.rules import Disp, Effect, Env, FactView
from erp_v2.billing_gl.settings import MapperSettings
from erp_v2.posting import rules as posting_rules

RECV, PREPAY, FUND, CASH, OPEN_CASH = (str(uuid4()) for _ in range(5))
SEAL = datetime(2026, 11, 1, tzinfo=UTC)
D = Decimal


def env(**kw) -> Env:
    base = {
        "functional_currency": "NGN",
        "receivable_account": RECV,
        "prepayment_account": PREPAY,
        "prepaid_funding_account": None,
        "cash_accounts": {"integrator": CASH, "opening_refund": OPEN_CASH},
        "seal_at": SEAL,
        "settlement_sources": ("integrator",),
        "reversed_kind": None,
    }
    return Env(**{**base, **kw})


def view(version, kind, effects=(), *, currency="NGN", committed_at=None) -> FactView:
    return FactView(
        version=version,
        kind=kind,
        fact_id=uuid4(),
        billing_account_id=uuid4(),
        currency=currency,
        occurred_at=SEAL,
        committed_at=committed_at,
        effects=tuple(Effect(lane, D(delta)) for lane, delta in effects),
    )


def decide(version, kind, effects=(), **kw):
    e = env(**{k: v for k, v in kw.items() if k in Env.__dataclass_fields__})
    v = view(version, kind, effects, **{k: x for k, x in kw.items() if k in ("currency", "committed_at")})
    return rules.decide(rules.rule_for(version, kind), v, e)


def net(decision, account):
    return sum((ln.debit - ln.credit for ln in decision.lines if ln.account_ref == account), D(0))


# ---------------------------------------------------------------- dispatch table


def _literal_args(cls, field):
    hints = typing.get_type_hints(cls)
    return set(typing.get_args(hints[field]))


def test_dispatch_is_exhaustive_over_the_billing_fact_literals() -> None:
    expected = {(1, kind) for kind in _literal_args(billing_contracts.AccountingFactV1, "effect_kind")} | {
        (2, kind) for kind in _literal_args(billing_contracts.AccountingFactV2, "effect_kind")
    }
    expected |= {(3, kind) for kind in _literal_args(billing_contracts.AccountingFactV3, "effect_kind")}
    expected |= {(4, kind) for kind in typing.get_args(opening_cash_contracts.OpeningCashPostingKind)}
    expected |= {(5, kind) for kind in _literal_args(opening_receivable_contracts.AccountingFactV5, "effect_kind")}
    expected |= {(6, kind) for kind in _literal_args(opening_variance_contracts.AccountingFactV6, "effect_kind")}
    assert set(rules.DISPATCH) == expected


def test_unknown_version_kind_blocks() -> None:
    decision = rules.decide(rules.rule_for(1, "invented"), view(1, "invented"), env())
    assert (decision.disposition, decision.reason) == (Disp.BLOCKED, "unknown_fact_kind")
    assert rules.rule_for(9, "allocation") is rules.UNKNOWN


def test_posting_bridge_has_exactly_the_one_reviewed_entry() -> None:
    assert ("billing", "accounting_fact") in posting_rules.POLICIES
    assert posting_rules.policy_for("billing", "accounting_fact").code_refs == frozenset()


@pytest.mark.parametrize("kind", ["invoice_issued", "credit_note_issued"])
def test_invoice_kinds_block_until_rev2(kind) -> None:
    decision = decide(1, kind, [("receivable", "100")])
    assert (decision.disposition, decision.reason) == (Disp.BLOCKED, "invoice_mapping_not_built")


@pytest.mark.parametrize(
    ("version", "kind"),
    [
        (2, "withholding_accepted"),
        (2, "withholding_reversed"),
        (3, "non_cash_credit_granted"),
        (3, "non_cash_credit_reduced"),
    ],
)
def test_v2_v3_are_read_through_the_public_getters_and_blocked_by_name(version, kind) -> None:
    rule = rules.rule_for(version, kind)
    assert rule.needs_fact is True  # R9 (billing a13): get_accounting_fact_v2 / _v3
    expected = "wht_mapping_not_built" if version == 2 else "non_cash_credit_mapping_not_built"
    decision = rules.decide(rule, view(version, kind), env())
    assert (decision.disposition, decision.reason) == (Disp.BLOCKED, expected)


# --------------------------------------------------------------------- lane math


def test_receipt_posts_dr_cash_cr_prepayment() -> None:
    decision = decide(1, "settlement_accepted", [("available_credit", "500.00")])
    assert decision.disposition == Disp.POST
    assert net(decision, CASH) == D("500.00") and net(decision, PREPAY) == D("-500.00")


def test_allocation_posts_dr_prepayment_cr_receivable() -> None:
    decision = decide(1, "allocation", [("available_credit", "-200.00"), ("receivable", "-200.00")])
    assert net(decision, PREPAY) == D("200.00") and net(decision, RECV) == D("-200.00")
    assert not any(ln.account_ref == CASH for ln in decision.lines)


def test_deallocation_is_the_mirror_and_refund_credits_cash() -> None:
    back = decide(1, "deallocation", [("available_credit", "200.00"), ("receivable", "200.00")])
    assert net(back, PREPAY) == D("-200.00") and net(back, RECV) == D("200.00")
    refund = decide(1, "refund", [("available_credit", "-50.00")])
    assert net(refund, PREPAY) == D("50.00") and net(refund, CASH) == D("-50.00")


def test_empty_lane_effects_are_lane_neutral_no_gl() -> None:
    decision = decide(1, "reallocation", [])
    assert (decision.disposition, decision.reason) == (Disp.NO_GL, "lane_neutral")


def test_unbalanced_lane_only_kind_blocks() -> None:
    decision = decide(1, "allocation", [("available_credit", "-200.00"), ("receivable", "-100.00")])
    assert (decision.disposition, decision.reason) == (Disp.BLOCKED, "lane_effects_unbalanced")


def test_reversal_moves_cash_only_for_settlement_family() -> None:
    effects = [("available_credit", "-500.00")]
    family = decide(1, "reversal", effects, reversed_kind="settlement_accepted")
    assert net(family, CASH) == D("-500.00")
    alloc = decide(1, "reversal", [("available_credit", "200"), ("receivable", "200")], reversed_kind="allocation")
    assert alloc.disposition == Disp.POST and not any(ln.account_ref == CASH for ln in alloc.lines)
    assert decide(1, "reversal", effects, reversed_kind="allocation").reason == "lane_effects_unbalanced"


def test_opening_refund_uses_its_own_cash_key() -> None:
    decision = decide(4, "opening_cash_refunded", [("available_credit", "-30.00")])
    assert net(decision, OPEN_CASH) == D("-30.00")
    unset = decide(4, "opening_cash_refunded", [("available_credit", "-30.00")], cash_accounts={"integrator": CASH})
    assert unset.reason == "cash_account_unmapped"


def test_opening_applied_and_draw_reversed_are_lane_only() -> None:
    applied = decide(4, "opening_cash_applied", [("available_credit", "-10"), ("receivable", "-10")])
    assert applied.disposition == Disp.POST and len(applied.lines) == 2
    assert decide(4, "cash_credit_draw_reversed", [("available_credit", "10"), ("receivable", "10")]).disposition == (
        Disp.POST
    )


# ----------------------------------------------------------------- blocks (T5, T6)


def test_prepaid_funding_blocks_while_unset_and_posts_when_set() -> None:
    effects = [("prepaid_funding", "75.00")]
    assert decide(1, "settlement_accepted", effects).reason == "prepaid_funding_unmapped"
    ok = decide(1, "settlement_accepted", effects, prepaid_funding_account=FUND)
    assert ok.disposition == Disp.POST and net(ok, FUND) == D("-75.00") and net(ok, CASH) == D("75.00")


def test_non_functional_currency_blocks() -> None:
    decision = decide(1, "settlement_accepted", [("available_credit", "5")], currency="USD")
    assert decision.reason == "billing_fx_unsupported"


def test_missing_accounts_and_settlement_block_with_stable_codes() -> None:
    effects = [("available_credit", "5")]
    assert decide(1, "settlement_accepted", effects, cash_accounts={}).reason == "cash_account_unmapped"
    assert decide(1, "settlement_accepted", effects, prepayment_account=None).reason == "prepayment_account_unmapped"
    assert decide(1, "settlement_accepted", effects, settlement_sources=()).reason == "settlement_unresolved"
    assert decide(1, "settlement_accepted", effects, settlement_sources=("a", "b")).reason == "cash_sources_ambiguous"
    assert decide(
        1, "allocation", [("receivable", "1"), ("available_credit", "-1")], receivable_account=None
    ).reason == ("receivable_account_unmapped")
    assert decide(1, "settlement_accepted", effects, functional_currency=None).reason == "functional_currency_unset"


# ------------------------------------------------------------------ T2 tie


def test_t2_tie_exact_at_every_step() -> None:
    """GL(prepayment) = available credit and GL(1400) = receivable lane after every fact (a billing position)."""
    gl: dict[str, Decimal] = {}
    position = {"receivable": D(0), "available_credit": D(0)}
    steps = [
        ("invoice", None),  # the receivable lane is created by the invoice (rev 2); seed it directly
        ("settlement_accepted", [("available_credit", "500.00")]),
        ("allocation", [("available_credit", "-300.00"), ("receivable", "-300.00")]),
        ("deallocation", [("available_credit", "100.00"), ("receivable", "100.00")]),
        ("refund", [("available_credit", "-50.00")]),
        ("reversal", [("available_credit", "50.00")]),
    ]
    position["receivable"] = D("400.00")
    gl[RECV] = D("400.00")  # the invoice's Dr 1400 (outside this rev's dispatch)
    for kind, effects in steps[1:]:
        reversed_kind = "refund" if kind == "reversal" else None
        decision = decide(1, kind, effects, reversed_kind=reversed_kind)
        assert decision.disposition == Disp.POST, kind
        for lane, delta in effects:
            position[lane] += D(delta)
        for ln in decision.lines:
            gl[ln.account_ref] = gl.get(ln.account_ref, D(0)) + ln.debit - ln.credit
        assert gl[RECV] == position["receivable"], kind
        assert -gl.get(PREPAY, D(0)) == position["available_credit"], kind
    assert sum(gl.values()) == D("400.00")  # only the seeded invoice debit (revenue side is rev 2) is unbalanced


# ------------------------------------------------------------ opening skips


def test_opening_facts_skip_only_when_committed_at_or_before_the_seal() -> None:
    before = decide(5, "opening_account_imported", committed_at=SEAL - timedelta(seconds=1))
    assert (before.disposition, before.reason) == (Disp.SKIP, "opening_gl_carried")
    at = decide(4, "opening_cash_imported", committed_at=SEAL)
    assert at.disposition == Disp.SKIP
    after = decide(5, "opening_account_imported", committed_at=SEAL + timedelta(seconds=1))
    assert after.reason == "opening_fact_after_seal"
    assert decide(4, "opening_cash_import_reversed", committed_at=SEAL - timedelta(days=1)).disposition == Disp.SKIP
    unwind = decide(5, "opening_account_import_reversed", committed_at=SEAL + timedelta(days=1))
    assert unwind.reason == "opening_unwind_post_seal"
    assert decide(5, "opening_account_imported", committed_at=SEAL, seal_at=None).reason == "opening_seal_unavailable"


# -------------------------------------------------------------- V6 (T3, N2)


@pytest.mark.parametrize("kind", sorted(k for v, k in rules.DISPATCH if v == 6))
def test_v6_is_refused_without_reading_approval_fields(kind) -> None:
    """Both the two-person shape and the a13/R4b OFF shape (null request id, no approvers) get the same
    disposition because the mapper never decodes a V6 fact: the rule says needs_fact False and the decision uses
    nothing but (version, kind)."""
    rule = rules.rule_for(6, kind)
    assert rule.needs_fact is False
    two_person = view(6, kind)
    window_off = view(6, kind, currency=None)
    a = rules.decide(rule, two_person, env())
    b = rules.decide(rule, window_off, env(seal_at=None, functional_currency=None))
    assert (a.disposition, a.reason) == (b.disposition, b.reason) == (Disp.REFUSED, "v6_ledger_lane_unmapped")
    assert a.lines == ()


# ----------------------------------------------------------- settings digest


def _settings(**kw) -> MapperSettings:
    base = {
        "enabled": False,
        "cash_accounts": {"integrator": CASH},
        "posting_timezone": "Africa/Lagos",
        "closed_period_policy": "next_open_period",
        "settle_seconds": 300,
        "resummarize_days": 45,
        "rescan_days": 7,
        "rescan_batch": 24,
        "history_floor": date(2000, 1, 1),
        "prepayment_account": PREPAY,
        "prepaid_funding_account": None,
    }
    return MapperSettings(**{**base, **kw})


def test_map_digest_tracks_mapping_values_only() -> None:
    base = _settings().map_digest
    assert len(base) == 64
    assert _settings(enabled=True, settle_seconds=1, resummarize_days=1).map_digest == base
    assert _settings(prepaid_funding_account=FUND).map_digest != base
    assert _settings(cash_accounts={"integrator": OPEN_CASH}).map_digest != base
    assert _settings(closed_period_policy="hold").map_digest != base


def test_period_bounds_read_in_the_posting_timezone() -> None:
    lo, hi = period_bounds_utc(date(2026, 1, 1), date(2026, 1, 31), "Africa/Lagos")
    assert lo == datetime(2025, 12, 31, 23, tzinfo=UTC) and hi == datetime(2026, 1, 31, 23, tzinfo=UTC)


# ------------------------------------------------------------------------- CLI


def test_cli_refuses_operator_dsns_a_bad_tenant_and_apply_without_the_switch(monkeypatch, capsys) -> None:
    from erp_v2.billing_gl import cli

    tenant = str(uuid4())
    assert cli.main(["run", "--tenant-id", "not-a-uuid"]) == 2
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://documentation.invalid/db")
    assert cli.main(["run", "--tenant-id", tenant]) == 2
    monkeypatch.delenv("MIGRATION_DATABASE_URL")
    monkeypatch.delenv(cli.APPLY_ENV, raising=False)
    assert cli.main(["run", "--tenant-id", tenant, "--apply"]) == 2
    assert cli.main(["run", "--tenant-id", tenant]) == 2  # no runtime DSN: refused, never guessed
    out = capsys.readouterr().out
    for code in ("invalid_tenant_id", "operator_database_materials_forbidden", "explicit_billing_gl_apply_required"):
        assert code in out
    assert "documentation.invalid" not in out and tenant not in out
