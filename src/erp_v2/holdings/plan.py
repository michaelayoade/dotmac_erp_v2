# ruff: noqa: E501
"""Holding plan (B14.5): read three times, assemble one sha-bound plan; ``plan`` writes nothing.

Transactions (``read_snapshot`` / ``read_billing_positions`` / ``recheck``):
1. REPEATABLE READ READ ONLY: GL per code, the owner subledgers, R1 and the ledger sha.
2. READ COMMITTED read-write, ROLLED BACK: billing per-account positions.
3. REPEATABLE READ READ ONLY again: R1 ``position_fingerprint`` and the ledger sha must be unchanged, else
   ``moved_during_plan``.

The plan lists, per control code x currency: GL, subledger, X, the items, the journal lines and the once-only document
identity. ``plan_sha256`` is the canonical sha over all of it plus the params, the source receipt shas, the window id and
the census sha. Currency is functional-only: ledger lines carry no transaction currency, so any other currency with an
amount refuses ``holding_fx_unsupported``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.contracts import PeriodStatus
from dotmac_accounting.models import FiscalPeriod, JournalEntry
from sqlalchemy.orm import Session

from erp_v2.holdings import attribution as attr
from erp_v2.holdings import positions
from erp_v2.holdings.attribution import ZERO, Census, GlAttribution, HoldingItem, Party
from erp_v2.holdings.billing import BillingReader, Position, R1Snapshot
from erp_v2.holdings.config import AccountRef, HoldingConfig, control_account_id, prepayment_account, resolve_config
from erp_v2.holdings.errors import refuse
from erp_v2.holdings.models import HOLDING_CODES, PREPAYMENT, holding_document_id
from erp_v2.migration_ledger.common import canonical_sha, day, fmt, sha_text, strict_json, text, uuid_text

ORDER: Final = ("1400", "2000", "2040", "2045", "1410", PREPAYMENT)
OPENING_OWNER: Final = "erp.opening"
SPEC_KEYS: Final = {"version", "tenant_id", "window_id", "capture_date", "evidence_ref", "params", "receipts"}
OwnerReader = Callable[[Session, UUID, str, str], Mapping[tuple[Party, str], Decimal]]


@dataclass(frozen=True, slots=True)
class ReceiptRef:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RunSpec:
    tenant_id: UUID
    window_id: str
    capture_date: date
    evidence_ref: str
    params: dict[str, Any]
    census: ReceiptRef | None
    gl_attribution: dict[str, ReceiptRef]


def parse_spec(raw: bytes) -> tuple[RunSpec, str]:
    """The run-spec: bound in the plan sha. Receipt refs are ``{"path": ..., "sha256": ...}``; the bytes are verified."""
    sha = hashlib.sha256(raw).hexdigest()
    body = strict_json(raw, "invalid_spec_json")
    if not isinstance(body, dict) or set(body) != SPEC_KEYS or body["version"] != 1:
        refuse("invalid_spec_schema")
    receipts = body["receipts"]
    if not isinstance(receipts, dict) or not set(receipts) <= {"census", "gl_attribution"}:
        refuse("invalid_spec_schema")
    params = body["params"]
    if not isinstance(params, dict):
        refuse("invalid_spec_schema")

    def ref(value: object) -> ReceiptRef:
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            refuse("invalid_spec_schema")
        return ReceiptRef(
            text(value["path"], "invalid_spec_schema", 500), sha_text(value["sha256"], "invalid_spec_schema")
        )

    attributions = receipts.get("gl_attribution", {})
    if not isinstance(attributions, dict) or not set(attributions) <= set(HOLDING_CODES) - {"1400", PREPAYMENT}:
        refuse("invalid_spec_schema")
    return (
        RunSpec(
            uuid_text(body["tenant_id"], "invalid_tenant_id"),
            text(body["window_id"], "invalid_spec_schema", 120),
            day(body["capture_date"], "invalid_capture_date"),
            text(body["evidence_ref"], "evidence_ref_required", 255),
            params,
            ref(receipts["census"]) if "census" in receipts else None,
            {c: ref(v) for c, v in attributions.items()},
        ),
        sha,
    )


# --- posting date (C5) --------------------------------------------------------------------------------------------


def posting_date(db: Session, tenant: UUID, capture: date) -> date:
    """The capture date if its period is OPEN and not adjustment; else the start of the first such period after it.
    Never earlier than the opening period's end (the posting date of the opening journal)."""
    floor = db.scalar(
        sa.select(sa.func.max(JournalEntry.posting_date)).where(
            JournalEntry.tenant_id == tenant, JournalEntry.source_owner == OPENING_OWNER
        )
    )
    start = max(capture, floor) if floor is not None else capture
    periods = list(
        db.scalars(select_periods(tenant).order_by(FiscalPeriod.start_date))  # type: ignore[arg-type]
    )
    for p in periods:
        if p.start_date <= start <= p.end_date and p.status == PeriodStatus.OPEN and not p.is_adjustment:
            return start
    for p in periods:
        if p.start_date > start and p.status == PeriodStatus.OPEN and not p.is_adjustment:
            return p.start_date
    return refuse("no_open_period", "no open period on or after the capture date")


def select_periods(tenant: UUID):
    return sa.select(FiscalPeriod).where(FiscalPeriod.tenant_id == tenant)


# --- snapshot -----------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Snapshot:
    functional: str
    config: HoldingConfig
    controls: dict[str, AccountRef]  # control code -> control account (prepayment -> the prepayment account)
    gl: dict[str, Decimal]
    t1_net_1400: Decimal
    owner_sub: dict[str, Mapping[tuple[Party, str], Decimal]]
    r1: R1Snapshot
    ledger_sha: str
    posting_date: date


def read_snapshot(
    db: Session, spec: RunSpec, billing: BillingReader, *, owner_readers: Mapping[str, OwnerReader] | None = None
) -> Snapshot:
    from erp_v2.journals.policy import functional_currency

    tenant = spec.tenant_id
    functional = functional_currency(db, tenant)
    config = resolve_config(db, tenant, spec.params)
    controls = {c: control_account_id(db, tenant, c) for c in ORDER if c != PREPAYMENT}
    controls[PREPAYMENT] = prepayment_account(db, tenant)
    gl = {c: positions.gl_net(db, tenant, a.id) for c, a in controls.items()}
    readers = dict(owner_readers or {})
    owner_sub: dict[str, Mapping[tuple[Party, str], Decimal]] = {}
    for code in ("2000", "2040", "2045", "1410"):
        owner_sub[code] = (
            readers[code](db, tenant, code, functional)
            if code in readers
            else positions.owner_sub(db, tenant, code, functional)
        )
    return Snapshot(
        functional, config, controls, gl, positions.t1_net_1400(db, tenant, controls["1400"].id), owner_sub,
        billing.r1(db), positions.v2_posted_sha(db, tenant), posting_date(db, tenant, spec.capture_date),
    )  # fmt: skip


# --- assembly -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Receipts:
    census: Census | None
    census_sha: str | None
    gl_attribution: dict[str, GlAttribution]
    shas: dict[str, str]


def _minor_dec(value: int) -> Decimal:
    return Decimal(value).scaleb(-attr.MINOR_EXPONENT)


def _check_functional(ccys: set[str], functional: str, code: str) -> None:
    if ccys - {functional}:
        refuse("holding_fx_unsupported", code)


def _journal_lines(items: list[HoldingItem], control: AccountRef, holding: AccountRef, label: str):
    from erp_v2.holdings import posting

    pos = sum((i.amount for i in items if i.amount > 0), ZERO)
    neg = -sum((i.amount for i in items if i.amount < 0), ZERO)
    return posting.gross_lines(control.id, holding.id, pos, neg, label=label)


def assemble(
    spec: RunSpec,
    spec_sha: str,
    snap: Snapshot,
    receipts: Receipts,
    positions_by_account: Mapping[tuple[str, str], Position],
    r1_recheck: R1Snapshot,
    ledger_sha_recheck: str,
    *,
    planned_by: str,
) -> dict[str, Any]:
    if r1_recheck.position_fingerprint != snap.r1.position_fingerprint or ledger_sha_recheck != snap.ledger_sha:
        refuse("moved_during_plan")
    f = snap.functional
    r1_cur = dict(snap.r1.currencies)
    _check_functional({c for c in r1_cur if _nonzero(r1_cur[c])}, f, "1400")
    acct_by_ccy: dict[str, dict[Party, Position]] = {}
    for (customer, ccy), pos in positions_by_account.items():
        acct_by_ccy.setdefault(ccy, {})[Party(attr.CENSUS_PARTY_KIND, customer)] = pos
    for ccy, per in acct_by_ccy.items():
        if ccy != f and any(_nonzero(p) for p in per.values()):
            refuse("holding_fx_unsupported", "billing")
        total = Position(*(sum(getattr(p, n) for p in per.values()) for n in (
            "collectible_minor", "non_cash_minor", "available_credit_minor", "prepaid_funding_minor")))  # fmt: skip
        if ccy in r1_cur and total != r1_cur[ccy]:
            refuse("billing_positions_mismatch", "per-account positions differ from R1")
    if set(acct_by_ccy) - set(r1_cur) and any(
        _nonzero(p) for c in set(acct_by_ccy) - set(r1_cur) for p in acct_by_ccy[c].values()
    ):
        refuse("billing_positions_mismatch", "per-account positions without an R1 currency")

    codes: list[dict[str, Any]] = []
    for code in ORDER:
        control = snap.controls[code]
        holding = snap.config.accounts[code]
        gl_total = snap.gl[code]
        if code in ("1400", PREPAYMENT):
            census = receipts.census
            if census is None:
                refuse("census_required", code)
            _check_functional(census.currencies(), f, code)
            att = attr.census_attribution(census, code=code, currency=f, functional=f)
            if code == "1400":
                if snap.t1_net_1400 != _minor_dec(census.cash_lot_total(f)):
                    refuse("t1_not_posted", "post T1 (ar_opening_credit) before the holding plan")
                pre_t1 = gl_total - snap.t1_net_1400
                if _minor_dec(census.gl_total_minor(f, f)) != pre_t1:
                    refuse("ar_attribution_stale", "census does not equal GL(1400) before T1")
            pos = r1_cur.get(f, Position(0, 0, 0, 0))
            if code == "1400":
                sub_total = _minor_dec(pos.receivable())
                sub = {p: _minor_dec(v.receivable()) for p, v in acct_by_ccy.get(f, {}).items()}
            else:
                sub_total = -_minor_dec(pos.credit())
                sub = {p: -_minor_dec(v.credit()) for p, v in acct_by_ccy.get(f, {}).items()}
        else:
            owner = snap.owner_sub[code]
            _check_functional({c for (_p, c), v in owner.items() if v != 0}, f, code)
            sub = {p: v for (p, c), v in owner.items() if c == f}
            sub_total = sum(sub.values(), ZERO)
            att = receipts.gl_attribution.get(code)
            if att is None:
                if gl_total != 0:
                    refuse("gl_attribution_required", code)
                att = GlAttribution(code, f, {}, {}, (), ZERO, "")
            if att.currency != f or att.code != code:
                refuse("holding_fx_unsupported" if att.currency != f else "invalid_gl_attribution", code)
            if att.gl_total() != gl_total:
                refuse(f"attribution_stale:{code}", "receipt does not equal the ledger")
        items = attr.itemise(
            attribution=att, sub=sub, gl_total=gl_total, sub_total=sub_total, holding_account_code=holding.code
        )
        if not items:
            continue
        pos_amt = sum((i.amount for i in items if i.amount > 0), ZERO)
        lines = _journal_lines(items, control, holding, f"Migration holding {code} {f}")
        codes.append(
            {
                "code": code,
                "currency": f,
                "gl": fmt(gl_total),
                "subledger": fmt(sub_total),
                "x": fmt(gl_total - sub_total),
                "control_account_id": str(control.id),
                "holding_account_id": str(holding.id),
                "holding_account_code": holding.code,
                "document_id": holding_document_id(spec.window_id, code, f),
                "items": [i.as_plan() for i in items],
                "lines": [
                    {"account_id": ln.account_ref, "debit": fmt(ln.debit), "credit": fmt(ln.credit)} for ln in lines
                ],
                "gross_positive": fmt(pos_amt),
            }
        )
    body = {
        "version": 1,
        "tenant_scope": hashlib.sha256(str(spec.tenant_id).encode()).hexdigest(),
        "window_id": spec.window_id,
        "spec_sha256": spec_sha,
        "params": spec.params,
        "config": snap.config.as_body(),
        "receipts": dict(sorted(receipts.shas.items())),
        "census_sha256": receipts.census_sha,
        "r1_fingerprint": snap.r1.position_fingerprint,
        "v2_posted_sha256": snap.ledger_sha,
        "functional_currency": f,
        "posting_date": snap.posting_date.isoformat(),
        "codes": codes,
    }
    return {"plan": body, "plan_sha256": canonical_sha(body), "planned_by": planned_by}


def _nonzero(p: Position) -> bool:
    return any((p.collectible_minor, p.non_cash_minor, p.available_credit_minor, p.prepaid_funding_minor))
