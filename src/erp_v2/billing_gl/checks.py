"""B-15 gate inputs and the period-close guard. Reads only the mapper's own tables (no billing import).

``ready`` is wired into the flip gate by the B-9r follow-up (B15.7), not here. ``period_blockers`` is called from
``journals.service.soft_close_period`` / ``lock_period``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp_v2.billing_gl.models import BLOCKED, PENDING, REFUSED, SKIP, FactPosting, FactWindow

V6_VERSION: Final = 6


def period_bounds_utc(start: date, end: date, timezone: str) -> tuple[datetime, datetime]:
    """Half-open UTC bounds of an inclusive date range read in the mapper's posting timezone."""
    tz = ZoneInfo(timezone)
    return (
        datetime.combine(start, time.min, tzinfo=tz).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC),
    )


def period_blockers(db: Session, tenant_id: UUID, start: date, end: date, timezone: str) -> dict[str, int]:
    """Counts that stop a period from closing: unclosed windows overlapping it, BLOCKED/WAITING facts in it."""
    lo, hi = period_bounds_utc(start, end, timezone)
    unclosed = (
        db.scalar(
            select(func.count())
            .select_from(FactWindow)
            .where(
                FactWindow.tenant_id == tenant_id,
                FactWindow.closed_at.is_(None),
                FactWindow.occurred_from < hi,
                FactWindow.occurred_to > lo,
            )
        )
        or 0
    )
    pending = (
        db.scalar(
            select(func.count())
            .select_from(FactPosting)
            .where(
                FactPosting.tenant_id == tenant_id,
                FactPosting.disposition.in_(PENDING),
                FactPosting.occurred_at >= lo,
                FactPosting.occurred_at < hi,
            )
        )
        or 0
    )
    return {"unclosed_windows": unclosed, "blocked_facts": pending}


def facts_incomplete(db: Session, tenant_id: UUID, start: date, end: date) -> bool:
    """True while an unclosed window overlaps the period or a BLOCKED/WAITING fact occurred in it."""
    from dotmac_kernel.settings_resolver import resolve_value

    from erp_v2.billing_gl import settings

    timezone = str(resolve_value(db, settings.DOMAIN, "posting_timezone", tenant_id=tenant_id, default="Africa/Lagos"))
    return any(period_blockers(db, tenant_id, start, end, timezone).values())


VAT_REASONS: Final = (
    "tax_account_unmapped",
    "revenue_account_unmapped",
    "line_accounts_mismatch",
    "vat_position_missing",
    "vat_invariant",
    "vat_allocation_unresolved",
    "invoice_document_unresolved",
)


def tax_accounts_mapped(tax_accounts: dict[str, dict[str, str]]) -> bool:
    """Rev 2 gate input: at least one treatment, each with both a current (1440) and a deferral (1455) account."""
    return bool(tax_accounts) and all(
        isinstance(v, dict) and v.get("current") and v.get("deferral") for v in tax_accounts.values()
    )


def ready(
    db: Session,
    tenant_id: UUID,
    *,
    now: datetime,
    settle_seconds: int,
    seal_at: datetime | None,
    tax_accounts: dict[str, dict[str, str]] | None = None,
) -> dict:
    """The B15.7 gate inputs. Each failing input is listed; ``ready`` is true only when none fails.

    1. every window up to now - settle exists and is closed; 2. 0 BLOCKED/WAITING; 3. 0 REFUSED V6;
    4. every pre-seal fact is SKIP; 5. no live fact before F10 (a POSTED row must not exist before the flip).
    """
    horizon = now - timedelta(seconds=settle_seconds)
    horizon_day = datetime.combine(horizon.date(), time.min, tzinfo=UTC)
    unclosed = db.scalar(
        select(func.count())
        .select_from(FactWindow)
        .where(FactWindow.tenant_id == tenant_id, FactWindow.closed_at.is_(None))
    )
    last_closed = db.scalar(
        select(func.max(FactWindow.occurred_to)).where(
            FactWindow.tenant_id == tenant_id, FactWindow.closed_at.isnot(None)
        )
    )
    pending = db.scalar(
        select(func.count())
        .select_from(FactPosting)
        .where(FactPosting.tenant_id == tenant_id, FactPosting.disposition.in_(PENDING))
    )
    refused_v6 = db.scalar(
        select(func.count())
        .select_from(FactPosting)
        .where(
            FactPosting.tenant_id == tenant_id,
            FactPosting.disposition == REFUSED,
            FactPosting.fact_version == V6_VERSION,
        )
    )
    pre_seal_not_skip = 0
    if seal_at is not None:
        pre_seal_not_skip = db.scalar(
            select(func.count())
            .select_from(FactPosting)
            .where(
                FactPosting.tenant_id == tenant_id,
                FactPosting.occurred_at <= seal_at,
                FactPosting.disposition != SKIP,
            )
        )
    posted = db.scalar(
        select(func.count())
        .select_from(FactPosting)
        .where(FactPosting.tenant_id == tenant_id, FactPosting.disposition == "POSTED")
    )
    vat_blocked = db.scalar(
        select(func.count())
        .select_from(FactPosting)
        .where(
            FactPosting.tenant_id == tenant_id,
            FactPosting.disposition.in_(PENDING),
            FactPosting.reason.in_(VAT_REASONS),
        )
    )
    report = {
        "vat_blocked": vat_blocked or 0,
        "tax_accounts_mapped": tax_accounts_mapped(tax_accounts or {}),
        "unclosed_windows": unclosed or 0,
        "windows_behind": last_closed is None or last_closed < horizon_day,
        "blocked_or_waiting": pending or 0,
        "refused_v6": refused_v6 or 0,
        "pre_seal_not_skip": pre_seal_not_skip or 0,
        "posted": posted or 0,
    }
    report["ready"] = not (
        report["unclosed_windows"]
        or report["windows_behind"]
        or report["blocked_or_waiting"]
        or report["refused_v6"]
        or report["pre_seal_not_skip"]
        or report["posted"]
        or report["vat_blocked"]
        or not report["tax_accounts_mapped"]
    )
    return report


__all__ = ["BLOCKED", "period_blockers", "ready"]
