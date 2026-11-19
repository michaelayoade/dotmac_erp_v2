"""The legacy side of the bridge, RECOMPUTED from the snapshot (never declared): ``gl.posted_ledger_line``.

[E] columns (g9 legacy copy): ``account_id`` (uuid), ``debit_amount`` / ``credit_amount`` (functional; the
``original_*`` columns are foreign currency and unused), ``posting_date`` (date) and ``journal_entry_id``. Legacy
account ids are v2 account ids, so the bridge is keyed on the id and shown by the CURRENT v2 code (a recode changes the
code, never the identity). Period = ``YYYY-MM`` of ``posting_date``. When an opening date is given, everything on or
before it is the ``OPENING`` period (the cumulative legacy balance) and the universe of legacy refs is the set of
journal ids posted after it. The connection is read-only (``SET TRANSACTION READ ONLY`` and ``row_security = off`` are
the caller's, exactly as B-2 does).
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


def recompute(
    legacy: sa.Connection, db: Session, tenant: UUID, *, opening_as_of: date | None
) -> tuple[dict[tuple[str, str], Decimal], frozenset[str]]:
    codes = account_codes(db, tenant)
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
        for account_id, net in legacy.execute(
            sa.text(
                "SELECT account_id::text, sum(debit_amount - credit_amount) FROM gl.posted_ledger_line "
                "WHERE posting_date <= :as_of GROUP BY 1"
            ),
            {"as_of": opening_as_of},
        ):
            if account_id not in codes:
                refuse("legacy_account_unmapped")
            key = (codes[account_id], OPENING_PERIOD)
            out[key] = out.get(key, ZERO) + Decimal(net)
    universe = frozenset(
        r[0]
        for r in legacy.execute(
            sa.text("SELECT DISTINCT journal_entry_id::text FROM gl.posted_ledger_line WHERE posting_date > :after"),
            {"after": after},
        )
    )
    return {k: v for k, v in out.items() if v != 0}, universe
