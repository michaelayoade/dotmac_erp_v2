"""IAS 8 posting mode for B1b (D-M5, E06f): a prior-period error posted in the first open period against 3100.

A supplier invoice dated in a closed prior period (the FY2025 invoice legacy never posted) is a prior-period
error, not a current-period expense. Under a Finance decision ``ias8_post`` the owner's accounting consequence
is kept as it is (the owner still recognises the liability and records its receipt against its own
consequence) and the journal that actually posts is derived from it:

* the posting date is the first day of the tenant's first OPEN, non-adjustment fiscal period, which must be
  later than the invoice date (otherwise it is not a prior-period item and the decision is refused);
* the expense-side debits (every debit that is not the line's tax account) go to the decision's account, which
  must be the tenant's account 3100; the tax debits (V1 declared input-tax account) and the credit to the
  liability account are untouched, so the control and tax rules are not bypassed and the journal still
  balances at the invoice total;
* the journal description carries the review id and the decision file digest as evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer.admission_waivers import IAS8_ACCOUNT_CODE

DOMAIN = "erp.b1b.ias8-consequence.v1"


class Ias8Refused(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def first_open_period_start(session: Session, tenant: UUID) -> date | None:
    from dotmac_accounting.contracts import PeriodStatus
    from dotmac_accounting.models import FiscalPeriod

    return session.scalar(
        sa.select(sa.func.min(FiscalPeriod.start_date)).where(
            FiscalPeriod.tenant_id == tenant,
            FiscalPeriod.status == PeriodStatus.OPEN,
            FiscalPeriod.is_adjustment.is_(False),
        )
    )


def posting_date_for(session: Session, tenant: UUID, invoice_date: date) -> date:
    """The IAS 8 posting date, or a named refusal. The invoice's own period must be genuinely closed to posting."""
    from dotmac_accounting.contracts import PeriodStatus
    from dotmac_accounting.models import FiscalPeriod

    own = list(
        session.scalars(
            sa.select(FiscalPeriod).where(
                FiscalPeriod.tenant_id == tenant,
                FiscalPeriod.start_date <= invoice_date,
                FiscalPeriod.end_date >= invoice_date,
            )
        )
    )
    if len(own) != 1:
        raise Ias8Refused("ias8_invoice_period_missing")
    if own[0].status == PeriodStatus.OPEN and not own[0].is_adjustment:
        raise Ias8Refused("ias8_period_open_use_normal_path")
    start = first_open_period_start(session, tenant)
    if start is None or start <= invoice_date:
        raise Ias8Refused("ias8_no_later_open_period")
    return start


def require_account(session: Session, tenant: UUID, account_id: str | None) -> str:
    """The decision's account must be the tenant's postable account 3100. Returns the canonical id."""
    from dotmac_accounting.contracts import AccountKind
    from dotmac_accounting.models import Account

    if account_id is None:
        raise Ias8Refused("ias8_account_missing")
    account = session.scalar(sa.select(Account).where(Account.tenant_id == tenant, Account.id == UUID(account_id)))
    if account is None or account.code != IAS8_ACCOUNT_CODE:
        raise Ias8Refused("ias8_account_not_3100")
    if not account.is_active or not account.posting_allowed or account.kind != AccountKind.POSTING:
        raise Ias8Refused("ias8_account_not_postable")
    return str(account.id)


def derive(
    consequence, *, tax_refs: frozenset[str], account_ref: str, posting_date: date, review_id: str, file_sha: str
):
    """The IAS 8 journal consequence derived from the owner's consequence (same source identity, same total)."""
    entries = tuple(
        replace(entry, account_ref=account_ref) if entry.debit > 0 and entry.account_ref not in tax_refs else entry
        for entry in consequence.entries
    )
    if entries == tuple(consequence.entries) or not any(e.account_ref == account_ref for e in entries):
        raise Ias8Refused("ias8_no_expense_entry")
    description = (
        f"{consequence.description} [IAS 8 prior-period correction; review {review_id}; decision {file_sha[:16]}]"
    )
    payload = dict(
        domain=DOMAIN,
        base=consequence.fingerprint,
        posting_date=posting_date.isoformat(),
        account_ref=account_ref,
        review_id=review_id,
        description=description,
        entries=[[e.account_ref, str(e.debit), str(e.credit)] for e in entries],
    )
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    debit = sum((Decimal(e.debit) for e in entries), Decimal(0))
    credit = sum((Decimal(e.credit) for e in entries), Decimal(0))
    if debit != credit:
        raise Ias8Refused("ias8_unbalanced")
    return replace(
        consequence, posting_date=posting_date, description=description, entries=entries, fingerprint=fingerprint
    )
