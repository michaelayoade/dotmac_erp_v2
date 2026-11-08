"""Tenant web login/logout over kernel identity storage.

Identity resolution mirrors the Starter reference: person Party by
(tenant, lower(normalized email)), credential by party, constant-work
dummy verification on any miss (no timing oracle), hash upgrade on
successful login, session issued as an AuthSession row keyed by the
token digest. ERP stores no passwords, sessions, or users of its own.
"""

from __future__ import annotations

from dotmac_kernel.identity import normalize_email
from dotmac_kernel.models import AuthSession, Party, PartyType, Tenant, UserCredential
from dotmac_kernel.security import (
    hash_password,
    hash_token,
    issue_access_token,
    password_needs_rehash,
    verify_password,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

_DUMMY_HASH = hash_password("erp-web-constant-work-dummy")


def web_login(db: Session, tenant: Tenant, email: str, password: str) -> str | None:
    """Return an access token on success, None on any credential failure."""

    normalized = normalize_email(email)
    party = db.scalars(
        select(Party)
        .where(Party.tenant_id == tenant.id)
        .where(Party.party_type == PartyType.person)
        .where(func.lower(Party.email) == normalized)
    ).first()
    credential = (
        db.scalars(
            select(UserCredential)
            .where(UserCredential.tenant_id == tenant.id)
            .where(UserCredential.party_id == party.id)
        ).first()
        if party is not None
        else None
    )
    if credential is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, credential.password_hash):
        return None
    if password_needs_rehash(credential.password_hash):
        credential.password_hash = hash_password(password)
    token, expires_at = issue_access_token(credential.party_id, tenant.id)
    db.add(
        AuthSession(
            tenant_id=tenant.id,
            party_id=credential.party_id,
            token_hash=hash_token(token),
            expires_at=expires_at,
        )
    )
    db.flush()
    return token


def web_logout(db: Session, tenant: Tenant, token: str | None) -> None:
    """Revoke the caller's own session; unknown tokens are a no-op."""

    if not token:
        return
    session = db.scalars(
        select(AuthSession).where(AuthSession.tenant_id == tenant.id).where(AuthSession.token_hash == hash_token(token))
    ).first()
    if session is not None:
        db.delete(session)
        db.flush()


__all__ = ["web_login", "web_logout"]
