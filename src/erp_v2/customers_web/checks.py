"""Customer screen gate check: ``ready(db, tenant)``. Wiring it into the flip gate is a follow-up (B-9r).

1. Setup: the account-number series setting is present (a blank number cannot be allocated without it).
2. Wave-4 completeness: every customer account has its finance row (the importer transform lands it).

Read-only. A report, never a repair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from erp_v2.customers_web import queries, settings


@dataclass(frozen=True, slots=True)
class Readiness:
    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def ready(db: Session, tenant_id: UUID) -> Readiness:
    reasons: list[str] = []
    if settings.series_code(db, tenant_id) is None:
        reasons.append(f"customers_setup_incomplete:{settings.KEY_SERIES_ACCOUNT_NUMBER}")
    missing = queries.missing_finance_count(db, tenant_id)
    if missing:
        reasons.append(f"customers_finance_missing:{missing}")
    return Readiness(not reasons, tuple(reasons))
