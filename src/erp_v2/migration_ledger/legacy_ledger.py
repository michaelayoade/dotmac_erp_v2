"""The legacy side of the bridge, RECOMPUTED from the snapshot (never declared): ``gl.posted_ledger_line``.

[E] columns (g9 legacy copy): ``account_id`` (uuid), ``debit_amount`` / ``credit_amount`` (functional; the
``original_*`` columns are foreign currency and unused), ``posting_date`` (date) and ``journal_entry_id``. Legacy
account ids are v2 account ids, so the bridge is keyed on the id and shown by the CURRENT v2 code (a recode changes the
code, never the identity). Period = ``YYYY-MM`` of ``posting_date``. When an opening date is given, everything on or
before it is the ``OPENING`` period (the cumulative legacy balance) and the universe of legacy refs is the set of
journal ids posted after it. The connection is read-only (``SET TRANSACTION READ ONLY`` and ``row_security = off`` are
the caller's, exactly as B-2 does).

P&L roll-up (12.3d): legacy never closed income and expense to retained earnings, so in ``OPENING`` every account whose
v2 class is REVENUE or EXPENSE carries 0 and its cumulative net to the opening date is added to 3100.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.models import Account
from sqlalchemy.orm import Session

from erp_v2.migration_ledger.common import OPENING_PERIOD, ZERO, refuse


def account_codes(db: Session, tenant: UUID) -> dict[str, str]:
    return {str(i): c for i, c in db.execute(sa.select(Account.id, Account.code).where(Account.tenant_id == tenant))}


def pnl_accounts(db: Session, tenant: UUID) -> set[str]:
    """Account ids whose v2 class is REVENUE (income) or EXPENSE: legacy never closed these to retained earnings."""
    from dotmac_accounting.contracts import AccountClass
    from dotmac_accounting.models import AccountCategory

    return {
        str(i)
        for i in db.scalars(
            sa.select(Account.id)
            .join(
                AccountCategory,
                (AccountCategory.tenant_id == Account.tenant_id) & (AccountCategory.id == Account.category_id),
            )
            .where(
                Account.tenant_id == tenant,
                AccountCategory.account_class.in_([AccountClass.REVENUE, AccountClass.EXPENSE]),
            )
        )
    }


RETAINED_EARNINGS = "3100"


def recompute(
    legacy: sa.Connection, db: Session, tenant: UUID, *, opening_as_of: date | None
) -> tuple[dict[tuple[str, str], Decimal], frozenset[str]]:
    codes = account_codes(db, tenant)
    undated = legacy.execute(sa.text("SELECT count(*) FROM gl.posted_ledger_line WHERE posting_date IS NULL")).scalar()
    if undated:
        # An undated ledger line would be omitted from BOTH the opening and the post-cutoff TB by the date filters.
        refuse("legacy_ledger_line_undated", f"{undated} legacy ledger lines carry no posting_date")
    after = opening_as_of or date.min
    rows = legacy.execute(
        sa.text(
            "SELECT account_id::text AS a, to_char(posting_date, 'YYYY-MM') AS p, "
            "sum(debit_amount - credit_amount) AS net FROM gl.posted_ledger_line "
            "WHERE posting_date > :after GROUP BY 1, 2"
        ),
        {"after": after},
    )
    out: dict[tuple[str, str], Decimal] = {}
    for account_id, period, net in rows:
        if account_id not in codes:
            refuse("legacy_account_unmapped", "a legacy account id does not exist in the destination chart")
        key = (codes[account_id], period)
        out[key] = out.get(key, ZERO) + Decimal(net)
    if opening_as_of is not None:
        pnl = pnl_accounts(db, tenant)
        rolled = ZERO
        for account_id, net in legacy.execute(
            sa.text(
                "SELECT account_id::text, sum(debit_amount - credit_amount) FROM gl.posted_ledger_line "
                "WHERE posting_date <= :as_of GROUP BY 1"
            ),
            {"as_of": opening_as_of},
        ):
            if account_id not in codes:
                refuse("legacy_account_unmapped")
            if account_id in pnl:
                # 12.3d: legacy never closed P&L to retained earnings. In OPENING an income or expense account
                # carries 0 and its cumulative net to the opening date is rolled into 3100.
                rolled += Decimal(net)
                continue
            key = (codes[account_id], OPENING_PERIOD)
            out[key] = out.get(key, ZERO) + Decimal(net)
        if rolled != 0:
            key = (RETAINED_EARNINGS, OPENING_PERIOD)
            out[key] = out.get(key, ZERO) + rolled
    universe = frozenset(
        r[0]
        for r in legacy.execute(
            sa.text("SELECT DISTINCT journal_entry_id::text FROM gl.posted_ledger_line WHERE posting_date > :after"),
            {"after": after},
        )
    )
    return {k: v for k, v in out.items() if v != 0}, universe
