"""Assembly-owned readiness route for the composed ERP shell.

The kernel directly mounts DB-free liveness at ``/health`` and already
bypasses tenant resolution for ``/health/ready``, but deliberately leaves the
readiness route itself to the assembly. Mounting this route through a
``FeatureManifest`` keeps it inside ``create_app``'s post-mount validation
instead of adding routes after the factory returns.

Readiness semantics: with no ``DATABASE_URL`` declared the shell has no
data dependency and is ready. Once a DSN is declared, readiness requires
the bound runtime to answer ``SELECT 1``; anything else is 503.
"""

from __future__ import annotations

import os
from typing import Final

import sqlalchemy as sa
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.session_runtime import (
    NoDatabaseRuntimeError,
    resolve_database_runtime,
)
from fastapi import APIRouter, Response

router = APIRouter(tags=["health"])


def readiness_probe() -> tuple[bool, str]:
    """Return (ready, mode). Never raises."""

    if "DATABASE_URL" not in os.environ:
        return True, "shell-no-database"
    try:
        runtime = resolve_database_runtime()
    except NoDatabaseRuntimeError:
        return False, "database-unbound"
    try:
        with runtime.engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except sa.exc.SQLAlchemyError:
        return False, "database-unreachable"
    return True, "database"


@router.get("/health/ready", include_in_schema=False)
def health_ready(response: Response) -> dict[str, str]:
    ready, mode = readiness_probe()
    if not ready:
        response.status_code = 503
        return {"status": "unavailable", "mode": mode}
    return {"status": "ok", "mode": mode}


readiness_feature: Final = FeatureManifest(
    name="erp_shell.readiness",
    routers=(router,),
)

__all__ = ["readiness_feature", "readiness_probe", "router"]
