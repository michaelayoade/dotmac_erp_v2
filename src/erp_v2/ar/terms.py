"""Due-date basis for a native invoice: VERIFIED, with every provenance field (billing requires it to collect).

``payment_terms_code`` / ``_version`` are the settings audit version id of the terms table the days came from
(supplied by the caller); ``derivation_policy`` is the ``erp_v2.ar.net_days`` rule, version ``1``. ``issued_at`` and
``effective_at`` are the invoice date at 00:00 Africa/Lagos; ``due_at`` is that plus the days.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotmac_billing.contracts import DueDateBasisStatus, DueDateBasisV1

from erp_v2.ar import constants

LAGOS = ZoneInfo(constants.TIMEZONE)


def midnight(day: date) -> datetime:
    """00:00 Africa/Lagos on ``day`` (the one time convention for every billing instant ERP supplies).

    Returned as the same instant in UTC: billing seals exposure evidence over the ISO rendering of its instants and
    tolerates only the as-read (UTC) and UTC renderings, so a Lagos-offset instant would fail its own seal check.
    """
    return datetime(day.year, day.month, day.day, tzinfo=LAGOS).astimezone(UTC)


def net_days_basis(*, invoice_date: date, terms_code: str, terms_version: str, evidence_ref: str) -> DueDateBasisV1:
    at = midnight(invoice_date)
    return DueDateBasisV1(
        status=DueDateBasisStatus.VERIFIED,
        source_authority="erp_v2.ar.terms",
        evidence_ref=evidence_ref,
        payment_terms_code=terms_code,
        payment_terms_version=terms_version,
        issued_at=at,
        effective_at=at,
        timezone=constants.TIMEZONE,
        derivation_policy=constants.DERIVATION_POLICY,
        derivation_version=constants.DERIVATION_VERSION,
    )


def due_at(invoice_date: date, days: int) -> datetime:
    return midnight(invoice_date + timedelta(days=days))


MANUAL_POLICY = "erp_v2.ar.manual_due_date"


def manual_basis(
    *, invoice_date: date, terms_code: str, terms_version: str, evidence_ref: str, actor_ref: str
) -> DueDateBasisV1:
    """A due date the preparer typed (legacy: each invoice carries its own due date). Recorded as ``manual``: the
    derivation policy names it and the override fields carry who set it."""
    at = midnight(invoice_date)
    return DueDateBasisV1(
        status=DueDateBasisStatus.VERIFIED,
        source_authority="erp_v2.ar.terms",
        evidence_ref=evidence_ref,
        payment_terms_code=terms_code,
        payment_terms_version=terms_version,
        issued_at=at,
        effective_at=at,
        timezone=constants.TIMEZONE,
        derivation_policy=MANUAL_POLICY,
        derivation_version=constants.DERIVATION_VERSION,
        override_actor=actor_ref,
        override_reason="manual due date",
        override_evidence_ref=evidence_ref,
    )
