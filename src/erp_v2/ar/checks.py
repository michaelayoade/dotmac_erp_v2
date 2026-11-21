"""AR producer gate check (3.8): ``ready(db, tenant)``. Wiring it into the flip gate waits for B-9r (follow-up).

1. Before F10: ``ar.producer.enabled`` is false and zero rows exist in the four B-16 tables.
2. Setup: every required setting key resolves, the series are enrolled in a collision domain (so a number cannot
   collide with a migrated ``document_number``) and the seller identity and payment terms are present. Otherwise
   ``ar_setup_incomplete:<key>``.
3. The S11 rehearsal receipt of the B-16 + B-15r2 PG lane is bound in the bridge body (not computed here; the bridge
   records it).

Read-only. A report, never a repair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from dotmac_kernel.cache import TenantScope
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp_v2.ar import numbering, settings
from erp_v2.ar.models import AR_TABLES, ArDraft, ArDraftLine, ArLineAccount, ArReceipt


@dataclass(frozen=True, slots=True)
class Readiness:
    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def row_counts(db: Session, tenant_id: UUID) -> dict[str, int]:
    return {
        model.__tablename__: db.scalar(select(func.count()).select_from(model).where(model.tenant_id == tenant_id)) or 0
        for model in (ArDraft, ArDraftLine, ArLineAccount, ArReceipt)
    }


def ready(db: Session, tenant_id: UUID) -> Readiness:
    reasons: list[str] = []
    if settings.producer_enabled(db, tenant_id):
        reasons.append("ar_producer_enabled_before_flip")
    reasons.extend(f"ar_rows_before_flip:{table}" for table, n in row_counts(db, tenant_id).items() if n)
    cfg = settings.load(db, tenant_id)
    reasons.extend(f"ar_setup_incomplete:{key}" for key in cfg.missing())
    scope = TenantScope(tenant_id)
    for key, series in (
        (settings.KEY_SERIES_INVOICE, cfg.series_invoice),
        (settings.KEY_SERIES_CREDIT_NOTE, cfg.series_credit_note),
        (settings.KEY_SERIES_RECEIPT, cfg.series_receipt),
    ):
        if series and not numbering.series_enrolled(db, scope=scope, series_code=series):
            reasons.append(f"ar_setup_incomplete:{key}:not_enrolled")
    return Readiness(not reasons, tuple(reasons))


__all__ = ["AR_TABLES", "Readiness", "ready", "row_counts"]
