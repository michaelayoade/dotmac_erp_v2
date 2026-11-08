"""Transport adapter for the Academy completion receiver.

Authentication is the deployment's existing Academy webhook HMAC
(`X-Webhook-Signature-256: sha256=<hex>` over the raw body), verified
before any envelope trust; the authenticated peer is then handed to
`deliver_authenticated`, which binds it to the envelope's claimed
source. The route is a platform-context route (tenant middleware
leaves `request.state.tenant` unset on the platform root host): the
destination tenant comes from deployment configuration, never from
the request, and the envelope's source scope must match the expected
Academy tenant slug before any session opens.

Configuration (environment, read per request; unset = not configured):
- `ACADEMY_SYNC_WEBHOOK_SECRET` — HMAC key shared with Academy.
- `ACADEMY_SYNC_TENANT_ID` — UUID of the ERP tenant that owns the
  Academy boundary in this deployment.
- `ACADEMY_SYNC_SOURCE_SCOPE_REF` — expected Academy tenant slug
  (default `dotmac`).
"""

from __future__ import annotations

import hashlib
import hmac
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
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.session_runtime import NoDatabaseRuntimeError, resolve_database_runtime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from erp_v2.academy_sync.contracts import OWNER_APPLICATION, SOURCE_APPLICATION, build_registry
from erp_v2.academy_sync.receiver import CompletionSyncReceiver

_SIGNATURE_HEADER: Final = "x-webhook-signature-256"
_MAX_BODY_BYTES: Final = 64 * 1024

router = APIRouter(tags=["integrations"])


def _refused(status_code: int, status: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"status": status})


def _expected_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@router.post("/integration/academy-sync/completions", include_in_schema=False)
async def receive_completion(request: Request) -> JSONResponse:
    if getattr(request.state, "tenant", None) is not None:
        # Platform-context route by construction; a tenant-resolved host
        # is not the integration surface.
        return _refused(404, "tenant_context_refused")
    secret = os.environ.get("ACADEMY_SYNC_WEBHOOK_SECRET", "")
    tenant_raw = os.environ.get("ACADEMY_SYNC_TENANT_ID", "")
    if not secret or not tenant_raw:
        return _refused(503, "not_configured")
    try:
        tenant_id = UUID(tenant_raw)
    except ValueError:
        return _refused(503, "not_configured")

    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return _refused(413, "body_too_large")
    supplied = request.headers.get(_SIGNATURE_HEADER, "")
    if not hmac.compare_digest(supplied, _expected_signature(secret, body)):
        return _refused(401, "bad_signature")

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
        runtime = resolve_database_runtime()
    except NoDatabaseRuntimeError:
        return _refused(503, "database_unbound")
    try:
        with runtime.tenant_session(tenant_id) as db:
            receipt = deliver_authenticated(
                body,
                peer=AuthenticatedPeer(application=SOURCE_APPLICATION, transport_subject="hmac:academy-sync"),
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

__all__ = ["academy_sync_feature", "router"]
