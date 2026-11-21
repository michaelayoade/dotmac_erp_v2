"""Billing's ``NumberingProvider`` over dotmac-numbering, plus the receipt number allocation.

Billing never imports numbering; the assembly supplies this adapter. A series enrolled in a collision domain (the
numbering ``replacement`` surface, newer than the a3 pin) takes the document identity as an immutable claim; a
plain series refuses claim arguments, so the adapter forwards the identity only when the series is enrolled. A
rendered-number or document collision is raised as the billing refusal ``document_number_collision`` (never retried).
At the a3 pin there is no ``replacement`` module: every series is plain and ``series_enrolled`` is False (the gate's
setup check then refuses until numbering is bumped in the composition PR).
"""

from __future__ import annotations

from datetime import date

from dotmac_billing.errors import BillingRuleViolation
from dotmac_kernel.cache import Scope
from dotmac_numbering.service import allocate
from sqlalchemy.orm import Session

try:  # pragma: no cover - exercised only with a numbering release that ships collision domains
    from dotmac_numbering.replacement import NumberCollision, read_series
except ImportError:  # the a3 pin
    NumberCollision = None  # type: ignore[assignment,misc]
    read_series = None  # type: ignore[assignment]


def series_enrolled(db: Session, *, scope: Scope, series_code: str) -> bool:
    """True when the series exists and is enrolled in a collision domain (the gate's setup check)."""
    if read_series is None:
        return False
    try:
        return read_series(db, scope=scope, series_code=series_code).domain is not None
    except Exception:
        return False


def _claim(db: Session, scope: Scope, series_code: str, document_reference: str) -> dict[str, str]:
    if read_series is None:
        return {}
    domain = read_series(db, scope=scope, series_code=series_code).domain
    return {"document_reference": document_reference} if domain is not None else {}


def allocate_number(
    db: Session,
    *,
    scope: Scope,
    series_code: str,
    reference_date: date,
    idempotency_key: str,
    document_reference: str,
) -> str:
    try:
        return allocate(
            db,
            scope=scope,
            series_code=series_code,
            reference_date=reference_date,
            idempotency_key=idempotency_key,
            **_claim(db, scope, series_code, document_reference),
        ).formatted_number
    except Exception as exc:
        if NumberCollision is not None and isinstance(exc, NumberCollision):
            raise BillingRuleViolation(
                "document_number_collision", "the next number is already used by another document"
            ) from exc
        raise


class Adapter:
    """``dotmac_billing.commands.NumberingProvider``."""

    def allocate(
        self,
        db: Session,
        *,
        scope: Scope,
        series_code: str,
        reference_date: date,
        idempotency_key: str,
        document_reference: str,
    ) -> str:
        return allocate_number(
            db,
            scope=scope,
            series_code=series_code,
            reference_date=reference_date,
            idempotency_key=idempotency_key,
            document_reference=document_reference,
        )
