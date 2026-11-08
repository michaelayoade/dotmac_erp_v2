"""Wave-0 shell proof: kernel factory boots the empty assembly; /health green.

No database is configured: the kernel keeps /health DB-free by design, so
liveness must answer 200 with DATABASE_URL unset.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from erp_v2.main import app


def test_health_is_green_without_database() -> None:
    with TestClient(app) as client:
        for path in ("/health", "/health/ready"):
            response = client.get(path)
            assert response.status_code == 200, path


def test_no_product_routes_mounted() -> None:
    # Health routes are schema-hidden; product APIs would appear in OpenAPI.
    paths = set(app.openapi()["paths"])
    assert not any(k in p for p in paths for k in ("billing", "payroll", "finance", "people"))
