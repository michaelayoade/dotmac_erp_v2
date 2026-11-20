# ruff: noqa: E501, S608
"""The holding-tie read (B14.1): a read-only check usable by the flip gate (the wiring is a separate, later commit).

For every holding account that has movements: GL(account) == sum of ``erp_migration_holdings`` amounts, items with an
outstanding amount are open and resolved items have none, and every ``migration_holding`` journal identity is posted
exactly once. It needs neither billing nor the owner subledgers (``holding verify`` adds the control ties).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.contracts import JournalStatus
from dotmac_accounting.models import JournalEntry
from sqlalchemy.orm import Session

from erp_v2.holdings import positions
from erp_v2.holdings.models import HOLDINGS_TABLE, KIND_HOLDING_POST, OWNER
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger.common import fmt


def holding_tie(db: Session, tenant: UUID) -> dict[str, Any]:
    accounts = []
    for account_id, total in db.execute(
        sa.text(
            f"SELECT holding_account_id, sum(amount) FROM public.{HOLDINGS_TABLE} WHERE tenant_id = :t GROUP BY 1 ORDER BY 1"
        ),
        {"t": tenant},
    ):
        gl = positions.gl_net(db, tenant, account_id)
        accounts.append(
            {"account_id": str(account_id), "gl": fmt(gl), "rows": fmt(Decimal(total)), "ok": gl == Decimal(total)}
        )
    bad = db.scalar(
        sa.text(
            "SELECT count(*) FROM (SELECT e.id FROM "
            f"public.{m.EXCEPTIONS_TABLE} e JOIN public.{HOLDINGS_TABLE} h ON h.exception_id = e.id WHERE e.tenant_id = :t "
            "GROUP BY e.id, e.state HAVING (e.state = 'open' AND sum(h.amount) = 0) "
            "OR (e.state = 'resolved' AND sum(h.amount) <> 0)) s"
        ),
        {"t": tenant},
    )
    docs = {
        d
        for (d,) in db.execute(
            sa.text(
                f"SELECT DISTINCT journal_source_document_id FROM public.{HOLDINGS_TABLE} WHERE tenant_id = :t AND kind = 'reclass'"
            ),
            {"t": tenant},
        )
    }
    posted = dict(
        db.execute(
            sa.select(JournalEntry.source_document_id, sa.func.count())
            .where(
                JournalEntry.tenant_id == tenant,
                JournalEntry.source_owner == OWNER,
                JournalEntry.source_document_kind == KIND_HOLDING_POST,
                JournalEntry.status == JournalStatus.POSTED,
            )
            .group_by(JournalEntry.source_document_id)
        ).all()
    )
    once = set(posted) == docs and all(n == 1 for n in posted.values())
    ok = all(a["ok"] for a in accounts) and not bad and once
    return {"ok": ok, "accounts": accounts, "item_state_violations": int(bad or 0), "identities_posted_once": once}
