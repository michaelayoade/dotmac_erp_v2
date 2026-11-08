"""Database binding: explicit, Postgres-only, non-bypassing posture."""

from __future__ import annotations

import pytest

from erp_v2 import database as database_module
from erp_v2.database import (
    DatabaseBindingError,
    build_database_runtime,
    database_startup_checks,
    enforce_runtime_posture,
    runtime_role_violations,
)


def test_absent_dsn_means_empty_shell_mode() -> None:
    assert build_database_runtime({}) is None
    assert database_startup_checks(None) == ()


def test_blank_dsn_refuses_instead_of_falling_back() -> None:
    with pytest.raises(DatabaseBindingError, match="blank"):
        build_database_runtime({"DATABASE_URL": "  "})


def test_non_postgres_dsn_refuses() -> None:
    with pytest.raises(DatabaseBindingError, match="PostgreSQL"):
        build_database_runtime({"DATABASE_URL": "sqlite:///tmp/x.db"})


def test_postgres_dsn_binds_without_connecting() -> None:
    runtime = build_database_runtime({"DATABASE_URL": "postgresql+psycopg://app_user:x@127.0.0.1:1/none"})
    assert runtime is not None
    assert len(database_startup_checks(runtime)) == 1


@pytest.mark.parametrize(
    ("bypasses", "superuser", "expected"),
    [(False, False, 0), (True, False, 1), (False, True, 1), (True, True, 2)],
)
def test_role_posture_matrix(bypasses: bool, superuser: bool, expected: int) -> None:
    problems = runtime_role_violations(bypasses_rls=bypasses, superuser=superuser)
    assert len(problems) == expected


def test_owner_material_refused_in_runtime_env() -> None:
    with pytest.raises(DatabaseBindingError, match="MIGRATION_DATABASE_URL"):
        build_database_runtime(
            {
                "DATABASE_URL": "postgresql+psycopg://app_user:x@127.0.0.1:1/none",
                "MIGRATION_DATABASE_URL": "postgresql+psycopg://app_admin:x@127.0.0.1:1/none",
            }
        )


def test_enforce_posture_raises_on_any_violation(monkeypatch) -> None:
    runtime = build_database_runtime({"DATABASE_URL": "postgresql+psycopg://app_user:x@127.0.0.1:1/none"})
    monkeypatch.setattr(database_module, "verify_runtime_role_posture", lambda engine: ["bypass"])
    with pytest.raises(DatabaseBindingError, match="bypass"):
        enforce_runtime_posture(runtime)
    enforce_runtime_posture(None)  # shell mode never raises
