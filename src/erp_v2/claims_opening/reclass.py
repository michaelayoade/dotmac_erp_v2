"""The 2045 claim-family reclass (S8 section 3): the set fingerprint, the line builder, the ONE journal per window.

The ONLY builder of the ``claim_family_reclass`` posting kind. Lines are system-built from the owner receipts that were
just written (never from operator input); the document identity is a constant-namespace uuid5 over (tenant, window,
"2045"); the posting bridge supplies idempotency, the open-period check and the atomic create+post. This is a system
door onto the control accounts 2040 (Dr) and 2045 (Cr), plus 2030 and 2110 (Dr): the approved plan's reclass.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid5

import sqlalchemy as sa
from dotmac_accounting.models import Account, JournalEntry, PostedLedgerLine
from dotmac_expenses import claimant_claims_balance, claims_payable_total
from dotmac_expenses.models import ClaimLiability, HistoricalClaimReceipt
from sqlalchemy.orm import Session

from erp_v2.claims_opening.reasons import FAMILY, FUNCTIONAL, ClaimsRefusal
from erp_v2.holdings import posting
from erp_v2.holdings.models import KIND_CLAIM_RECLASS, OWNER
from erp_v2.holdings.positions import gl_net
from erp_v2.migration_ledger.common import canonical_sha, fmt

CLAIMS_NS = UUID("5b0d1f0e-3f0a-5c7e-9b6a-0b14c0de0006")
SET_DOMAIN = "erp.claims-opening.set.v1"
CLAIMANT_PREFIX = "claimant:"


@dataclass(frozen=True)
class Admitted:
    legacy_claim_id: str
    claim_id: str
    liability_id: str
    residual: Decimal
    carried_code: str
    claimant_party_id: str
    employee_id: str | None = None  # the legacy employee id (private; for the 2040 fragment only)


def document_id(tenant: UUID, window_id: str) -> str:
    return str(uuid5(CLAIMS_NS, f"{tenant}:{window_id}:2045"))


def set_fingerprint(items: list[Admitted], *, currency: str, snapshot_sha256: str) -> str:
    """Order-independent. Everything in it is derivable from the owner receipts (the gate recomputes it from them)."""
    claims = sorted(
        [i.legacy_claim_id, i.claim_id, i.liability_id, fmt(i.residual), i.carried_code, i.claimant_party_id]
        for i in items
    )
    total = sum((i.residual for i in items), Decimal(0))
    return canonical_sha(
        dict(
            domain=SET_DOMAIN,
            claims=claims,
            count=len(items),
            sum=fmt(total),
            currency=currency,
            snapshot=snapshot_sha256,
        )
    )


def account_ids(db: Session, tenant: UUID) -> dict[str, UUID]:
    """code -> Account.id for 2045 and the family; exactly one account per code or it refuses."""
    out: dict[str, UUID] = {}
    for code in ("2045", *FAMILY):
        ids = list(db.scalars(sa.select(Account.id).where(Account.tenant_id == tenant, Account.code == code)))
        if len(ids) != 1:
            raise ClaimsRefusal(f"account_missing:{code}")
        out[code] = ids[0]
    return out


def build_lines(items: list[Admitted], accounts: dict[str, UUID]) -> tuple[posting.Line, ...]:
    """Dr one line per carried account (Σ residual), Cr 2045 one line per claimant. Balanced by construction."""
    if not items:
        raise ClaimsRefusal("reclass_empty_set")
    debit: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    credit: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for i in items:
        if i.carried_code not in FAMILY or i.residual <= 0:
            raise ClaimsRefusal("reclass_item_invalid")
        debit[i.carried_code] += i.residual
        credit[i.claimant_party_id] += i.residual
    lines = [
        posting.Line(str(accounts[code]), amount, Decimal(0), f"carried:{code}")
        for code, amount in sorted(debit.items())
    ]
    lines += [
        posting.Line(str(accounts["2045"]), Decimal(0), amount, f"{CLAIMANT_PREFIX}{party}")
        for party, amount in sorted(credit.items())
    ]
    if sum(x.debit for x in lines) != sum(x.credit for x in lines):
        raise ClaimsRefusal("reclass_unbalanced")
    return tuple(lines)


def build(tenant: UUID, window_id: str, items: list[Admitted], accounts: dict[str, UUID], *, posting_date: date,
          snapshot_sha256: str) -> posting.Consequence:  # fmt: skip
    return posting.build(
        kind=KIND_CLAIM_RECLASS,
        document_id=document_id(tenant, window_id),
        fingerprint=set_fingerprint(items, currency=FUNCTIONAL, snapshot_sha256=snapshot_sha256),
        posting_date=posting_date,
        description="Claim-family reclass to 2045 (historical claims opening)",
        currency=FUNCTIONAL,
        lines=build_lines(items, accounts),
    )


def posted_reclass(db: Session, tenant: UUID) -> JournalEntry | None:
    """The tenant's posted claim-family reclass journal (any window), or None."""
    rows = list(
        db.scalars(
            sa.select(JournalEntry).where(
                JournalEntry.tenant_id == tenant,
                JournalEntry.source_owner == OWNER,
                JournalEntry.source_document_kind == KIND_CLAIM_RECLASS,
            )
        )
    )
    if len(rows) > 1:
        raise ClaimsRefusal("reclass_not_unique")
    return rows[0] if rows else None


def admitted_from_receipts(db: Session, tenant: UUID) -> tuple[list[Admitted], str | None]:
    """Every admitted claim read from the owner rows (receipt + liability), and the one snapshot sha they share."""
    code_of = {str(i): c for i, c in db.execute(sa.select(Account.id, Account.code).where(Account.tenant_id == tenant))}
    rows = db.execute(
        sa.select(HistoricalClaimReceipt, ClaimLiability)
        .join(
            ClaimLiability,
            (ClaimLiability.tenant_id == HistoricalClaimReceipt.tenant_id)
            & (ClaimLiability.id == HistoricalClaimReceipt.liability_id),
        )
        .where(HistoricalClaimReceipt.tenant_id == tenant)
    ).all()
    items, snapshots = [], set()
    for receipt, liability in rows:
        snapshots.add(receipt.source_snapshot_sha256)
        carried = code_of.get(receipt.carried_account_ref)
        if carried is None:
            raise ClaimsRefusal("carried_account_unknown")
        items.append(
            Admitted(
                receipt.source_claim_id,
                str(receipt.claim_id),
                str(receipt.liability_id),
                Decimal(receipt.residual),
                carried,
                str(liability.claimant_party_id),
            )
        )
    if len(snapshots) > 1:
        raise ClaimsRefusal("claims_multiple_snapshots")
    return items, (next(iter(snapshots)) if snapshots else None)


def recomputed_fingerprint(db: Session, tenant: UUID) -> str | None:
    """set_fp recomputed from the owner receipts, or None when there are none."""
    items, snapshot = admitted_from_receipts(db, tenant)
    if not items:
        return None
    return set_fingerprint(items, currency=FUNCTIONAL, snapshot_sha256=snapshot or "")


def postcondition(db: Session, tenant: UUID, journal_id: UUID, accounts: dict[str, UUID]) -> None:
    """GL(2045) == -claims_payable_total == -Σ residual, and each claimant's Cr == their owner balance."""
    items, _ = admitted_from_receipts(db, tenant)
    total = sum((i.residual for i in items), Decimal(0))
    ref = str(accounts["2045"])
    ok = (
        gl_net(db, tenant, accounts["2045"])
        == -total
        == -claims_payable_total(db, tenant_id=tenant, liability_account_ref=ref)
    )
    per = defaultdict(lambda: Decimal(0))
    for description, amount in db.execute(
        sa.select(PostedLedgerLine.description, sa.func.sum(PostedLedgerLine.credit - PostedLedgerLine.debit))
        .where(
            PostedLedgerLine.tenant_id == tenant,
            PostedLedgerLine.journal_id == journal_id,
            PostedLedgerLine.account_id == accounts["2045"],
        )
        .group_by(PostedLedgerLine.description)
    ):
        per[str(description)] = Decimal(amount)
    claimants = {i.claimant_party_id for i in items}
    ok = ok and set(per) == {f"{CLAIMANT_PREFIX}{c}" for c in claimants}
    for claimant in claimants:
        ok = ok and per[f"{CLAIMANT_PREFIX}{claimant}"] == claimant_claims_balance(
            db, tenant_id=tenant, claimant_party_id=UUID(claimant), currency_code=FUNCTIONAL
        )
    if not ok:
        raise ClaimsRefusal("reclass_postcondition_failed")


def post(db: Session, tenant: UUID, consequence: posting.Consequence, *, approval_ref: str, actor: str, now=None):
    """Post the reclass once. An identity already posted with another fingerprint refuses ``version_conflict``."""
    return posting.post(db, tenant, consequence, approval_ref=approval_ref, actor=actor, now=now)
