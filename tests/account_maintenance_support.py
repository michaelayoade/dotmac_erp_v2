"""Governed account maintenance for tests (accounting a3: ac_0004).

From accounting 0.1.0a3 a plain ``UPDATE`` of an account's code, kind, posting flag or active flag is refused by the
database; the change must be recorded through the owner's command. These helpers call those commands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import ActivateAccount, RecodeAccount, RetireAccount
from dotmac_accounting.models import Account
from dotmac_kernel.cache import TenantScope
from sqlalchemy.orm import Session

REASON = "test fixture account maintenance"
ACTOR = "test-fixture"


def _scope(db: Session, account_id: UUID) -> TenantScope:
    return TenantScope(db.scalar(sa.select(Account.tenant_id).where(Account.id == account_id)))


def _common(db: Session, account_id: UUID) -> dict:
    return {
        "scope": _scope(db, account_id),
        "idempotency_key": f"test-account-maintenance-{uuid4()}",
        "idempotency_expires_at": None,
        "changed_at": datetime.now(UTC),
    }


def retire(db: Session, account_id: UUID) -> None:
    """Inactive and non-posting through ``retire_account`` (no balance, no draft use)."""
    accounting.retire_account(
        db,
        command=RetireAccount(account_id=account_id, reason=REASON, actor=ACTOR, open_subledger_items=0),
        **_common(db, account_id),
    )


def activate(db: Session, account_id: UUID, *, posting_allowed: bool = True) -> None:
    accounting.activate_account(
        db,
        command=ActivateAccount(account_id=account_id, reason=REASON, actor=ACTOR, posting_allowed=posting_allowed),
        **_common(db, account_id),
    )


def recode(db: Session, account_id: UUID, new_code: str) -> None:
    accounting.recode_account(
        db,
        command=RecodeAccount(account_id=account_id, new_code=new_code, reason=REASON, actor=ACTOR),
        **_common(db, account_id),
    )
