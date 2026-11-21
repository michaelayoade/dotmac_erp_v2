"""Database reads for the holding tools: GL nets, the owner subledgers per party, the T1 and holding postings.

Everything is a read; nothing writes. Signs are net = debit - credit: a liability subledger is NEGATIVE here.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.models import Account, JournalEntry, PostedLedgerLine
from sqlalchemy.orm import Session

from erp_v2.holdings.attribution import ZERO, Party
from erp_v2.holdings.models import KIND_T1_POST, OWNER

_OPEN_OBLIGATION: Final = ("OPEN", "PARTIALLY_SETTLED")
_OPEN_LIABILITY: Final = ("outstanding", "partially_settled")


def gl_net(db: Session, tenant: UUID, account_id: UUID) -> Decimal:
    return Decimal(
        db.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0)).where(
                PostedLedgerLine.tenant_id == tenant, PostedLedgerLine.account_id == account_id
            )
        )
    )


def posted_net_by_kind(db: Session, tenant: UUID, account_id: UUID, kind: str) -> Decimal:
    """Net of the account's lines posted by ``erp.migration`` journals of this kind."""
    return Decimal(
        db.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0))
            .join(
                JournalEntry,
                (JournalEntry.tenant_id == PostedLedgerLine.tenant_id)
                & (JournalEntry.id == PostedLedgerLine.journal_id),
            )
            .where(
                PostedLedgerLine.tenant_id == tenant,
                PostedLedgerLine.account_id == account_id,
                JournalEntry.source_owner == OWNER,
                JournalEntry.source_document_kind == kind,
            )
        )
    )


def t1_net_1400(db: Session, tenant: UUID, account_id: UUID) -> Decimal:
    return posted_net_by_kind(db, tenant, account_id, KIND_T1_POST)


def v2_posted_sha(db: Session, tenant: UUID) -> str:
    """The sha of the whole destination ledger (the ``moved_during_plan`` witness): ledger_check's recomputation."""
    from erp_v2.migration_ledger import ledger_check

    total, _native = ledger_check.v2_posted(db, tenant)
    from erp_v2.migration_ledger.common import canonical_sha, fmt

    return canonical_sha([[a, p, fmt(v)] for (a, p), v in sorted(total.items())])


# --- owner subledgers per party (GL-signed: liabilities negative) --------------------------------------------------


def sub_2000(db: Session, tenant: UUID) -> dict[tuple[Party, str], Decimal]:
    """AP: open obligations minus APPROVED credit-note availability, per supplier and currency."""
    out: dict[tuple[Party, str], Decimal] = {}
    owed = db.execute(
        sa.text(
            "SELECT si.supplier_ref, po.currency_code, coalesce(sum(po.outstanding_amount), 0) "
            "FROM mod_payables.payment_obligations po "
            "JOIN mod_payables.supplier_invoices si ON si.tenant_id = po.tenant_id AND si.id = po.invoice_id "
            "WHERE po.tenant_id = :t AND po.status::text = ANY(:o) GROUP BY 1, 2"
        ),
        {"t": tenant, "o": list(_OPEN_OBLIGATION)},
    )
    for ref, ccy, amount in owed:
        out[(Party("supplier", str(ref)), str(ccy))] = -Decimal(amount)
    credits = db.execute(
        sa.text(
            "SELECT supplier_ref, currency_code, coalesce(sum(available_amount), 0) FROM mod_payables.credit_notes "
            "WHERE tenant_id = :t AND status::text = 'APPROVED' GROUP BY 1, 2"
        ),
        {"t": tenant},
    )
    for ref, ccy, amount in credits:
        key = (Party("supplier", str(ref)), str(ccy))
        out[key] = out.get(key, ZERO) + Decimal(amount)
    return out


def sub_2040(db: Session, tenant: UUID) -> dict[tuple[Party, str], Decimal]:
    """Payroll: open liabilities on 2040 (the account id or the code 2040), per beneficiary (employee) and currency."""
    ids = [str(i) for i in db.scalars(sa.select(Account.id).where(Account.tenant_id == tenant, Account.code == "2040"))]
    rows = db.execute(
        sa.text(
            "SELECT beneficiary_type, beneficiary_ref, currency_code, coalesce(sum(amount - amount_settled), 0) "
            "FROM mod_payroll.payroll_liabilities WHERE tenant_id = :t AND status = ANY(:s) "
            "AND liability_account_ref = ANY(:r) GROUP BY 1, 2, 3"
        ),
        {"t": tenant, "s": list(_OPEN_LIABILITY), "r": [*ids, "2040"]},
    )
    return {(Party(str(kind), str(ref)), str(ccy)): -Decimal(total) for kind, ref, ccy, total in rows}


def sub_1410(db: Session, tenant: UUID, functional: str) -> dict[tuple[Party, str], Decimal]:
    from erp_v2.staff_loans import queries

    return {
        (Party("employee", ref), functional): Decimal(b.balance)
        for ref, b in queries.all_balances(db, tenant_id=tenant).items()
    }


def sub_2045(db: Session, tenant: UUID) -> dict[tuple[Party, str], Decimal]:
    """Claims (expenses a2): the open liabilities on the 2045 account, per claimant (employee party) and currency."""
    from dotmac_expenses import claim_liability_outstanding, open_claim_liabilities

    ids = {str(i) for i in db.scalars(sa.select(Account.id).where(Account.tenant_id == tenant, Account.code == "2045"))}
    out: dict[tuple[Party, str], Decimal] = {}
    for row in open_claim_liabilities(db, tenant_id=tenant):
        if row.liability_account_ref not in ids:
            continue
        key = (Party("employee", str(row.claimant_party_id)), row.currency_code)
        out[key] = out.get(key, ZERO) - claim_liability_outstanding(row)
    return out


OWNER_READERS: Final = {"2000": sub_2000, "2040": sub_2040, "2045": sub_2045}


def owner_sub(db: Session, tenant: UUID, code: str, functional: str) -> dict[tuple[Party, str], Decimal]:
    if code == "1410":
        return sub_1410(db, tenant, functional)
    return OWNER_READERS[code](db, tenant)


def totals_by_currency(positions: Mapping[tuple[Party, str], Decimal]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for (_p, ccy), v in positions.items():
        out[ccy] = out.get(ccy, ZERO) + v
    return out
