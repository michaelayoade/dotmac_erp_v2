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
# The floor may be earlier than this, never later: a late floor would hide the earlier days from the gate.
HISTORY_FLOOR_MAX: Final = date(2000, 1, 1)


def period_bounds_utc(start: date, end: date, timezone: str) -> tuple[datetime, datetime]:
    """Half-open UTC bounds of an inclusive date range read in the mapper's posting timezone."""
    tz = ZoneInfo(timezone)
    return (
        datetime.combine(start, time.min, tzinfo=tz).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC),
    )


def _day_floor(moment: datetime) -> datetime:
    return datetime.combine(moment.astimezone(UTC).date(), time.min, tzinfo=UTC)


def missing_days(
    db: Session, tenant_id: UUID, first_day: date, end_day: date, *, now: datetime, rescan_days: int
) -> int:
    """UTC days in [first_day, end_day) with NO closed window checked within ``rescan_days``.

    A day with no window row is missing (lag, the last day before the horizon, the per-run window cap, a
    ``history_floor`` set too late), not silently passed. Completeness is a presence check, never an absence check.
    """
    if end_day <= first_day:
        return 0
    lo = datetime.combine(first_day, time.min, tzinfo=UTC)
    hi = datetime.combine(end_day, time.min, tzinfo=UTC)
    fresh = db.scalar(
        select(func.count())
        .select_from(FactWindow)
        .where(
            FactWindow.tenant_id == tenant_id,
            FactWindow.occurred_from >= lo,
            FactWindow.occurred_from < hi,
            FactWindow.closed_at.isnot(None),
            func.coalesce(FactWindow.rechecked_at, FactWindow.closed_at) >= now - timedelta(days=rescan_days),
        )
    )
    return (end_day - first_day).days - (fresh or 0)


def horizon_day(now: datetime, settle_seconds: int) -> date:
    """The first UTC day NOT yet complete at ``now - settle``: days strictly before it must all be windowed."""
    horizon = now - timedelta(seconds=settle_seconds)
    day = horizon.astimezone(UTC).date()
    if datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC) <= horizon.astimezone(UTC):
        day += timedelta(days=1)
    return day


def period_blockers(
    db: Session,
    tenant_id: UUID,
    start: date,
    end: date,
    timezone: str,
    *,
    now: datetime | None = None,
    settle_seconds: int = 300,
    rescan_days: int = 7,
) -> dict[str, int]:
    """Counts that stop a period from closing.

    ``missing_windows`` counts every UTC day of the period (up to the settle horizon) that has no closed, recently
    rescanned window, whether or not a window row exists for it.
    """
    now = now or datetime.now(UTC)
    del settle_seconds  # the horizon is implicit: windows only exist for days before it
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
    last = hi.date() if hi == _day_floor(hi) else hi.date() + timedelta(days=1)
    # Days at or after the settle horizon cannot have a closed window yet, so they count as missing: a period cannot be
    # closed before its end plus settle (a fact that occurs later would otherwise be re-dated into the next period).
    end_day = last
    missing = missing_days(db, tenant_id, lo.date(), end_day, now=now, rescan_days=rescan_days)
    return {"unclosed_windows": unclosed, "blocked_facts": pending, "missing_windows": missing}


def facts_incomplete(db: Session, tenant_id: UUID, start: date, end: date, *, now: datetime | None = None) -> bool:
    """True while a day of the period has no fresh closed window, a window is unclosed, or a fact is BLOCKED."""
    from dotmac_kernel.settings_resolver import resolve_value

    from erp_v2.billing_gl import settings

    def read(key: str, default: object) -> object:
        return resolve_value(db, settings.DOMAIN, key, tenant_id=tenant_id, default=default)

    # The guard only applies to a tenant whose mapper is in use (enabled, or it has ever recorded a window or a fact);
    # a tenant that never turned it on has no billing facts to complete, so main stays deployable with the mapper idle.
    in_use = read("enabled", False) is True or bool(
        db.scalar(select(FactWindow.id).where(FactWindow.tenant_id == tenant_id).limit(1))
        or db.scalar(select(FactPosting.id).where(FactPosting.tenant_id == tenant_id).limit(1))
    )
    if not in_use:
        return False
    blockers = period_blockers(
        db,
        tenant_id,
        start,
        end,
        str(read("posting_timezone", "Africa/Lagos")),
        now=now,
        settle_seconds=int(read("settle_seconds", 300)),  # type: ignore[call-overload]
        rescan_days=int(read("rescan_days", 7)),  # type: ignore[call-overload]
    )
    return any(blockers.values())


def ready(
    db: Session,
    tenant_id: UUID,
    *,
    now: datetime,
    settle_seconds: int,
    seal_at: datetime | None,
    history_floor: date,
    rescan_days: int = 7,
    max_floor: date = HISTORY_FLOOR_MAX,
) -> dict:
    """The B15.7 gate inputs. Each failing input is listed; ``ready`` is true only when none fails.

    1. EVERY UTC day from ``history_floor`` to now - settle has a closed window rescanned within ``rescan_days``
    (a missing day fails, it is not skipped) and the floor is not later than the maximum;
    2. 0 BLOCKED/WAITING; 3. 0 REFUSED V6;
    4. every pre-seal fact is SKIP; 5. no live fact before F10 (a POSTED row must not exist before the flip).
    """
    unclosed = db.scalar(
        select(func.count())
        .select_from(FactWindow)
        .where(FactWindow.tenant_id == tenant_id, FactWindow.closed_at.is_(None))
    )
    missing = missing_days(
        db, tenant_id, history_floor, horizon_day(now, settle_seconds), now=now, rescan_days=rescan_days
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
    report = {
        "unclosed_windows": unclosed or 0,
        "missing_windows": missing,
        "floor_too_late": history_floor > max_floor,
        "blocked_or_waiting": pending or 0,
        "refused_v6": refused_v6 or 0,
        "pre_seal_not_skip": pre_seal_not_skip or 0,
        "posted": posted or 0,
    }
    report["ready"] = not (
        report["unclosed_windows"]
        or report["missing_windows"]
        or report["floor_too_late"]
        or report["blocked_or_waiting"]
        or report["refused_v6"]
        or report["pre_seal_not_skip"]
        or report["posted"]
    )
    return report


__all__ = ["BLOCKED", "period_blockers", "ready"]
