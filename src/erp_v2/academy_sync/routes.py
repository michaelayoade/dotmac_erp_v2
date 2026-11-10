"""Transport adapter for the Academy completion receiver.

Authentication is the kernel's machine credential: the caller presents an
`X-Api-Key` that resolves (inside the configured tenant's RLS scope) to a
non-revoked, unexpired, attributed credential holding exactly the scope
`academy_sync.completion:write`. Scope matching is exact set membership; there
is no wildcard and no shared secret. Every refusal is a fixed, opaque body.
The credential's `source_application` becomes the authenticated peer handed
to `deliver_authenticated`, which binds it to the envelope's claimed source.

The route is a platform-context route (tenant middleware leaves
`request.state.tenant` unset on the platform root host). Machine credentials
live behind tenant RLS, so the tenant scope is established from deployment
configuration, never from the request, before the key is looked up.

Configuration (environment, read per request; unset = not configured):
- `ACADEMY_SYNC_TENANT_ID` -- UUID of the ERP tenant that owns the
  Academy boundary in this deployment (not a secret).
- `ACADEMY_SYNC_SOURCE_SCOPE_REF` -- expected Academy tenant slug
  (default `dotmac`).
- `MACHINE_CREDENTIAL_HMAC_KEY` -- the kernel's dedicated verification
  secret, installed at startup by `erp_v2.machine_keys`.
"""

from __future__ import annotations

import json
import os
from typing import Final
from uuid import UUID

from dotmac_app_sync import (
    AuthenticatedPeer,
    EnvelopeInvalid,
    PeerMismatch,
    SyncAcceptance,
    SyncContractError,
    UnknownContract,
    deliver_authenticated,
)
from dotmac_kernel.exceptions import UnauthorizedError
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.machine_auth import API_KEY_HEADER, MachineKeyUnavailableError, authenticate_machine
from dotmac_kernel.session_runtime import NoDatabaseRuntimeError, resolve_database_runtime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from erp_v2.academy_sync.contracts import OWNER_APPLICATION, build_registry
from erp_v2.academy_sync.receiver import CompletionSyncReceiver

#: The one scope that admits this route (exact match, no wildcard).
REQUIRED_SCOPE: Final = "academy_sync.completion:write"
_MAX_BODY_BYTES: Final = 64 * 1024

router = APIRouter(tags=["integrations"])


def _refused(status_code: int, status: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"status": status})


@router.post("/integration/academy-sync/completions", include_in_schema=False)
async def receive_completion(request: Request) -> JSONResponse:
    if getattr(request.state, "tenant", None) is not None:
        # Platform-context route by construction; a tenant-resolved host
        # is not the integration surface.
        return _refused(404, "tenant_context_refused")
    tenant_raw = os.environ.get("ACADEMY_SYNC_TENANT_ID", "")
    if not tenant_raw:
        return _refused(503, "not_configured")
    try:
        tenant_id = UUID(tenant_raw)
    except ValueError:
        return _refused(503, "not_configured")

    raw_key = request.headers.get(API_KEY_HEADER, "")
    if not raw_key:
        return _refused(401, "unauthorized")
    try:
        runtime = resolve_database_runtime()
    except NoDatabaseRuntimeError:
        return _refused(503, "database_unbound")
    try:
        with runtime.tenant_session(tenant_id) as auth_db:
            principal = authenticate_machine(auth_db, raw_key)
    except MachineKeyUnavailableError:
        return _refused(503, "not_configured")
    except UnauthorizedError:
        return _refused(401, "unauthorized")
    if principal.tenant_id != tenant_id:
        return _refused(401, "unauthorized")
    if not principal.has_scope(REQUIRED_SCOPE):
        return _refused(403, "forbidden")

    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return _refused(413, "body_too_large")

    # Scope pre-check only (selects configuration; the envelope itself is
    # validated by deliver_authenticated below).
    try:
        scope = json.loads(body).get("source_scope") or {}
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return _refused(400, "envelope_invalid")
    expected_ref = os.environ.get("ACADEMY_SYNC_SOURCE_SCOPE_REF", "dotmac")
    if scope.get("kind") != "tenant" or scope.get("ref") != expected_ref:
        return _refused(422, "source_scope_refused")

    try:
        with runtime.tenant_session(tenant_id) as db:
            receipt = deliver_authenticated(
                body,
                peer=AuthenticatedPeer(
                    application=principal.application,
                    transport_subject=f"machine:{principal.credential_id}",
                ),
                expected_owner=OWNER_APPLICATION,
                registry=build_registry(),
                receiver=CompletionSyncReceiver(db, tenant_id=tenant_id),
            )
    except PeerMismatch:
        return _refused(401, "peer_mismatch")
    except EnvelopeInvalid:
        return _refused(400, "envelope_invalid")
    except UnknownContract:
        return _refused(404, "unknown_capability")
    except SyncContractError:
        return _refused(400, "contract_error")

    content = {
        "status": receipt.acceptance.value,
        "destination_ref": receipt.destination_ref,
        "error_code": receipt.error_code,
    }
    return JSONResponse(status_code=422 if receipt.acceptance is SyncAcceptance.REJECTED else 200, content=content)


academy_sync_feature: Final = FeatureManifest(
    name="erp_academy_sync",
    routers=(router,),
)

__all__ = ["REQUIRED_SCOPE", "academy_sync_feature", "router"]
