# ruff: noqa: E501
"""Post-flip holding tools (B14.7): ``release`` (paired with an owner command) and ``writeoff`` (governed).

``release`` runs in ONE REPEATABLE READ transaction (a serialization failure retries up to 3 times, then refuses):
lock the item's exception row; owner adapter PREVIEW (party and the subledger delta); post the ``holding_release``
journal FIRST (so the #63 opening bound holds); execute the owner command; re-read the party's subledger (the actual delta
must equal the preview); insert the ``release`` row with the owner receipt sha; assert GL(code) == subledger(code);
resolve the item ``admitted`` when its outstanding reaches 0. Any refusal rolls everything back.

Sign convention (net = debit - credit). An item's outstanding O = sum of its holding rows: O > 0 means the holding account
holds a DEBIT balance. The control differed from the subledger by D (= the reclass amount; O == D until released). The
owner command adds ``delta`` to the party's subledger (GL-signed): that closes the difference only if ``delta`` has the
SAME sign as D and ``|delta| <= |O|`` (``release_amount_invalid`` otherwise). The release row amount is ``-delta`` (opposite
in sign to the reclass, as the table trigger requires); the journal moves the control by ``delta`` and the holding by
``-delta``.

``writeoff``: full outstanding only (re-derived; no typed amount); the offset account must be postable and neither a
control, tax-role nor holding account, and in the allowlist when one is set.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from erp_v2.governance import policy as governance
from erp_v2.holdings import config as cfg
from erp_v2.holdings import plan as planning
from erp_v2.holdings import positions, posting
from erp_v2.holdings.attribution import ZERO, Party
from erp_v2.holdings.errors import refuse
from erp_v2.holdings.models import (
    KIND_RECLASS,
    KIND_RELEASE,
    KIND_RELEASE_POST,
    KIND_WRITEOFF,
    KIND_WRITEOFF_POST,
    PREPAYMENT,
    SOURCE_KIND,
    Holding,
    release_document_id,
    writeoff_document_id,
)
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import canonical_sha, fmt, sha_text, text

MAX_ATTEMPTS: Final = 3
SERIALIZATION_FAILURE: Final = "40001"
RR = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"


@dataclass(frozen=True, slots=True)
class OwnerPreview:
    party: Party
    delta: Decimal  # the party's subledger change, GL-signed
    detail: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OwnerReceipt:
    sha256: str


class HoldingOwnerAdapter(Protocol):
    control_code: str

    def preview(self, db: Session, tenant: UUID, *, command: Mapping[str, Any]) -> OwnerPreview: ...

    def execute(
        self, db: Session, tenant: UUID, *, command: Mapping[str, Any], preview: OwnerPreview
    ) -> OwnerReceipt: ...

    def party_subledger(self, db: Session, tenant: UUID, party: Party) -> Decimal:
        """The party's subledger on this control, GL-signed (functional currency)."""
        ...

    def subledger_total(self, db: Session, tenant: UUID) -> Decimal:
        """The whole subledger on this control, GL-signed (functional currency): the post-condition's right side."""


class RefusingAdapter:
    """An owner that cannot release yet: it refuses with a stable code (never improvises)."""

    def __init__(self, control_code: str, code: str) -> None:
        self.control_code, self._code = control_code, code

    def preview(self, db, tenant, *, command):
        return refuse(self._code)

    def execute(self, db, tenant, *, command, preview):
        return refuse(self._code)

    def party_subledger(self, db, tenant, party):
        return refuse(self._code)

    def subledger_total(self, db, tenant):
        return refuse(self._code)


def default_adapters() -> dict[str, HoldingOwnerAdapter]:
    """2000 (#67 reviewed admission) and 1410 (#63 opening) bind through ``register``; until bound they refuse
    ``release_owner_unbound``. Claims and payroll refuse ``release_owner_unavailable`` until S9 pins them; 1400 and
    prepayment refuse ``ar_release_requires_r8``."""
    return {
        "2000": RefusingAdapter("2000", "release_owner_unbound:2000"),
        "1410": RefusingAdapter("1410", "release_owner_unbound:1410"),
        "2040": RefusingAdapter("2040", "release_owner_unavailable"),
        "2045": RefusingAdapter("2045", "release_owner_unavailable"),
        "1400": RefusingAdapter("1400", "ar_release_requires_r8"),
        PREPAYMENT: RefusingAdapter(PREPAYMENT, "ar_release_requires_r8"),
    }


# --- item state ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemState:
    exception: m.MigrationException
    reclass: Holding
    outstanding: Decimal
    releases: int

    @property
    def party(self) -> Party | None:
        raw = (self.exception.detail or {}).get("party")
        return None if not raw else Party(str(raw["kind"]), str(raw["key"]))


def lock_item(db: Session, tenant: UUID, key: str) -> ItemState:
    rows = list(
        db.scalars(
            sa.select(m.MigrationException)
            .where(
                m.MigrationException.tenant_id == tenant,
                m.MigrationException.legacy_key == key,
                m.MigrationException.source_kind == SOURCE_KIND,
            )
            .with_for_update()
        )
    )
    if not rows:
        refuse("exception_unknown")
    if len(rows) > 1:
        refuse("exception_ambiguous", "the key names more than one holding item")
    exc = rows[0]
    holdings = list(db.scalars(sa.select(Holding).where(Holding.tenant_id == tenant, Holding.exception_id == exc.id)))
    reclass = next((h for h in holdings if h.kind == KIND_RECLASS), None)
    if reclass is None:
        refuse("item_not_reclassed")
    outstanding = sum((h.amount for h in holdings), ZERO)
    if exc.state != m.STATE_OPEN or outstanding == 0:
        refuse("item_not_open")
    return ItemState(exc, reclass, outstanding, sum(1 for h in holdings if h.kind == KIND_RELEASE))


def _control_ref(db: Session, tenant: UUID, code: str) -> cfg.AccountRef:
    return cfg.prepayment_account(db, tenant) if code == PREPAYMENT else cfg.control_account_id(db, tenant, code)


def _is_serialization(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    return (
        getattr(orig, "sqlstate", None) == SERIALIZATION_FAILURE
        or getattr(orig, "pgcode", None) == SERIALIZATION_FAILURE
    )


def with_retry(attempt: Callable[[], dict[str, Any]], *, attempts: int = MAX_ATTEMPTS) -> dict[str, Any]:
    """Run ``attempt`` (a whole transaction); a serialization failure retries, then refuses."""
    for _ in range(attempts):
        try:
            return attempt()
        except DBAPIError as exc:
            if not _is_serialization(exc):
                raise
    return refuse("serialization_retries_exhausted")


# --- release ------------------------------------------------------------------------------------------------------


def release_once(
    db: Session,
    tenant: UUID,
    key: str,
    command: Mapping[str, Any],
    *,
    adapters: Mapping[str, HoldingOwnerAdapter],
    operator: str,
    on_date: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One attempt, inside a transaction the caller opened at REPEATABLE READ and will commit or roll back."""
    operator = text(operator, "operator_ref_required")
    item = lock_item(db, tenant, key)
    code = item.reclass.control_code
    adapter = adapters.get(code) or refuse("release_owner_unavailable")
    preview = adapter.preview(db, tenant, command=command)
    if item.party is None or preview.party != item.party:
        refuse("release_amount_invalid", "the owner command does not name the item's party")
    delta = preview.delta
    if delta == 0 or (delta > 0) != (item.reclass.amount > 0) or abs(delta) > abs(item.outstanding):
        refuse("release_amount_invalid", "the owner delta does not close the item's difference")
    before = adapter.party_subledger(db, tenant, preview.party)
    control = _control_ref(db, tenant, code)
    n = item.releases + 1
    release_amount = -delta
    magnitude = abs(delta)
    holding = item.reclass.holding_account_id
    # delta > 0: the control GL gains a debit; delta < 0: a credit. The holding takes the opposite side.
    lines = (
        posting.Line(
            str(control.id), magnitude if delta > 0 else ZERO, ZERO if delta > 0 else magnitude, "Holding release"
        ),
        posting.Line(
            str(holding), ZERO if delta > 0 else magnitude, magnitude if delta > 0 else ZERO, "Holding release"
        ),
    )
    doc = release_document_id(item.exception.id, n)
    fingerprint = canonical_sha(
        {
            "exception": str(item.exception.id),
            "n": n,
            "delta": fmt(delta),
            "control": str(control.id),
            "holding": str(holding),
        }
    )
    consequence = posting.build(
        kind=KIND_RELEASE_POST,
        document_id=doc,
        fingerprint=fingerprint,
        posting_date=planning.posting_date(db, tenant, on_date),
        description=f"Migration holding release {code} {item.exception.legacy_key}"[:2000],
        currency=item.reclass.currency,
        lines=lines,
    )
    result = posting.post(
        db, tenant, consequence, approval_ref=f"holding-release:{fingerprint}", actor=operator, now=now
    )
    receipt = adapter.execute(db, tenant, command=command, preview=preview)
    sha_text(receipt.sha256, "owner_receipt_required")
    actual = adapter.party_subledger(db, tenant, preview.party) - before
    if actual != delta:
        refuse("owner_delta_mismatch", "the owner command changed the subledger by a different amount")
    db.add(
        Holding(
            tenant_id=tenant,
            exception_id=item.exception.id,
            control_code=code,
            currency=item.reclass.currency,
            holding_account_id=holding,
            kind=KIND_RELEASE,
            amount=release_amount,
            journal_id=result.journal_id,
            journal_source_document_id=doc,
            owner_receipt_sha256=receipt.sha256,
            planned_by=operator,
            applied_by=operator,
            dual_control=False,
        )
    )
    db.flush()
    _postcondition(db, tenant, adapter, code, control)
    remaining = item.outstanding + release_amount
    if remaining == 0:
        register.resolve(
            db, tenant, class_code=item.exception.class_code, legacy_key=item.exception.legacy_key,
            outcome="admitted", evidence_sha256=receipt.sha256,
        )  # fmt: skip
    return {"ready": True, "kind": KIND_RELEASE, "exception_state": "resolved" if remaining == 0 else "open",
            "remaining": fmt(remaining), "journal_id": str(result.journal_id), "owner_receipt_sha256": receipt.sha256}  # fmt: skip


def _postcondition(db: Session, tenant: UUID, adapter: HoldingOwnerAdapter, code: str, control: cfg.AccountRef) -> None:
    """GL(code) == the owner subledger total. 1400 and prepayment are proven by ``holding verify`` (R1)."""
    if code in ("1400", PREPAYMENT):
        return
    if positions.gl_net(db, tenant, control.id) != adapter.subledger_total(db, tenant):
        refuse("release_postcondition_failed", code)


def _functional(db: Session, tenant: UUID) -> str:
    from erp_v2.journals.policy import functional_currency

    return functional_currency(db, tenant)


def release(
    engine: Engine,
    tenant: UUID,
    key: str,
    command: Mapping[str, Any],
    *,
    adapters: Mapping[str, HoldingOwnerAdapter] | None = None,
    operator: str,
    on_date: date | None = None,
) -> dict[str, Any]:
    adapters = adapters or default_adapters()
    day = on_date or datetime.now(UTC).date()

    def attempt() -> dict[str, Any]:
        with Session(engine) as db:
            db.execute(sa.text(RR))
            try:
                out = release_once(db, tenant, key, command, adapters=adapters, operator=operator, on_date=day)
                db.commit()
                return out
            except BaseException:
                db.rollback()
                raise

    return with_retry(attempt)


# --- writeoff -----------------------------------------------------------------------------------------------------


def _offset_account(db: Session, tenant: UUID, code: str, allowlist: tuple[str, ...]) -> cfg.AccountRef:
    from dotmac_accounting.models import Account

    from erp_v2.ledger_roles import tax_role_account_ids

    ref = cfg.resolve_account(db, tenant, code, what="offset account")
    if ref.code != code:
        refuse("offset_account_invalid", "the offset account must be named by its code")
    barred = set(cfg.TIE_CONTROL_CODES)
    prepayment = cfg.prepayment_account(db, tenant)
    holding_ids = set(db.scalars(sa.select(Holding.holding_account_id).where(Holding.tenant_id == tenant).distinct()))
    if (
        ref.code in barred
        or ref.id == prepayment.id
        or str(ref.id) in tax_role_account_ids(db, tenant)
        or ref.id in holding_ids
    ):
        refuse("offset_account_invalid", "a control, tax-role or holding account cannot be the offset")
    if allowlist and ref.code not in allowlist:
        refuse("offset_account_not_allowed")
    _ = Account
    return ref


def plan_writeoff(
    db: Session,
    tenant: UUID,
    key: str,
    offset_code: str,
    evidence_sha256: str,
    *,
    allowlist: tuple[str, ...],
    planned_by: str,
) -> dict[str, Any]:
    sha_text(evidence_sha256, "evidence_sha_required")
    item = lock_item(db, tenant, key)
    offset = _offset_account(db, tenant, offset_code, allowlist)
    body = {
        "version": 1,
        "exception_id": str(item.exception.id),
        "outstanding": fmt(item.outstanding),
        "offset_account_id": str(offset.id),
        "holding_account_id": str(item.reclass.holding_account_id),
        "control_code": item.reclass.control_code,
        "currency": item.reclass.currency,
        "evidence_sha256": evidence_sha256,
    }
    return {"plan": body, "plan_sha256": canonical_sha(body), "planned_by": text(planned_by, "operator_ref_required")}


def apply_writeoff(
    db: Session,
    tenant: UUID,
    key: str,
    plan: dict[str, Any],
    *,
    approved_sha: str,
    applied_by: str,
    on_date: date,
    now=None,
) -> dict[str, Any]:
    """Caller opens the transaction. The plan is re-derived here and must equal the approved sha."""
    sha_text(approved_sha, "plan_sha_required")
    body = plan["plan"]
    item = lock_item(db, tenant, key)
    fresh = {**body, "outstanding": fmt(item.outstanding)}
    if canonical_sha(fresh) != approved_sha or plan["plan_sha256"] != approved_sha:
        refuse("plan_sha_mismatch", "the writeoff plan no longer matches the item")
    applied_by = text(applied_by, "operator_ref_required")
    planned_by = text(plan["planned_by"], "operator_ref_required")
    dual = governance.dual_control(db, tenant)
    if dual and planned_by == applied_by:
        refuse("dual_control_requires_distinct_operators")
    outstanding = item.outstanding
    magnitude = abs(outstanding)
    holding = item.reclass.holding_account_id
    offset = UUID(body["offset_account_id"])
    # outstanding > 0: the holding holds a debit; clear it with a credit and debit the offset.
    lines = (
        posting.Line(
            str(offset),
            magnitude if outstanding > 0 else ZERO,
            ZERO if outstanding > 0 else magnitude,
            "Holding writeoff",
        ),
        posting.Line(
            str(holding),
            ZERO if outstanding > 0 else magnitude,
            magnitude if outstanding > 0 else ZERO,
            "Holding writeoff",
        ),
    )
    doc = writeoff_document_id(item.exception.id)
    consequence = posting.build(
        kind=KIND_WRITEOFF_POST,
        document_id=doc,
        fingerprint=approved_sha,
        posting_date=planning.posting_date(db, tenant, on_date),
        description=f"Migration holding writeoff {item.exception.legacy_key}"[:2000],
        currency=item.reclass.currency,
        lines=lines,
    )
    result = posting.post(
        db, tenant, consequence, approval_ref=f"holding-writeoff:{approved_sha}", actor=applied_by, now=now
    )
    db.add(
        Holding(
            tenant_id=tenant,
            exception_id=item.exception.id,
            control_code=item.reclass.control_code,
            currency=item.reclass.currency,
            holding_account_id=holding,
            kind=KIND_WRITEOFF,
            amount=-outstanding,
            journal_id=result.journal_id,
            journal_source_document_id=doc,
            evidence_sha256=body["evidence_sha256"],
            planned_by=planned_by,
            applied_by=applied_by,
            dual_control=dual,
        )
    )
    db.flush()
    register.resolve(
        db, tenant, class_code=item.exception.class_code, legacy_key=item.exception.legacy_key,
        outcome="written_off", evidence_sha256=body["evidence_sha256"],
    )  # fmt: skip
    return {"ready": True, "kind": KIND_WRITEOFF, "journal_id": str(result.journal_id), "written_off": fmt(outstanding)}


def digest_command(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
