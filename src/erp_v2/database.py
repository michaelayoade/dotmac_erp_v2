"""Explicit database runtime binding for the composed ERP shell.

Posture (register S11): the runtime DSN must authenticate as a
non-superuser, NOBYPASSRLS role. Legacy ERP's online role posture varies by plane;
this assembly is born unable to bypass RLS. Superuser is checked as well as the flag,
because a superuser bypasses RLS regardless of ``rolbypassrls`` (kernel
``migrations/verify.py`` makes the same point for the named roles).

Binding rules:
- ``DATABASE_URL`` absent -> no runtime (empty-shell mode; health only).
- ``DATABASE_URL`` present but blank or non-Postgres -> refuse at import.
  A supplied-but-useless DSN is a configuration defect, never a reason
  to fall back to the kernel reference runtime.
- Role posture is verified by a startup check when a runtime is bound;
  the check connects, so it runs in the lifespan, not at import.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Final

import sqlalchemy as sa
from dotmac_kernel.assembly import StartupCheck
from dotmac_kernel.session_runtime import DatabaseRuntime

APPLICATION_NAME: Final = "dotmac-erp-v2"

_POSTGRES_SCHEMES: Final = frozenset({"postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"})


class DatabaseBindingError(RuntimeError):
    """A declared database binding is unusable. Sanitized: never carries a DSN."""


def _checked_url(raw: str, *, name: str) -> sa.URL:
    try:
        url = sa.make_url(raw)
    except sa.exc.ArgumentError as exc:
        raise DatabaseBindingError(f"{name} is set but is not a parseable database URL") from exc
    if url.get_dialect().name not in {"postgresql"} and url.drivername not in _POSTGRES_SCHEMES:
        raise DatabaseBindingError(
            f"{name} must be a PostgreSQL DSN (got scheme {url.drivername!r}); "
            "RLS posture is meaningless on any other backend"
        )
    return url


def build_database_runtime(
    environ: Mapping[str, str] | None = None,
) -> DatabaseRuntime | None:
    """Build the deployment's runtime from environment DSNs, or None.

    Engines are lazy: construction opens no connection. Identity and
    posture are proven later by ``runtime_posture_check``.
    """

    env = os.environ if environ is None else environ
    # Defense in depth: owner/source credentials never belong in a
    # runtime process environment (descriptor material separation).
    for forbidden in ("MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL"):
        if forbidden in env:
            raise DatabaseBindingError(f"{forbidden} must not be present in a runtime process")
    if "DATABASE_URL" not in env:
        return None
    raw = env["DATABASE_URL"]
    if not raw.strip():
        raise DatabaseBindingError(
            "DATABASE_URL is set but blank; unset it for empty-shell mode or supply the runtime DSN"
        )
    url = _checked_url(raw, name="DATABASE_URL")

    platform_raw = env.get("PLATFORM_DATABASE_URL")
    if platform_raw is not None and not platform_raw.strip():
        raise DatabaseBindingError("PLATFORM_DATABASE_URL is set but blank; unset it or supply it")
    platform_url = _checked_url(platform_raw, name="PLATFORM_DATABASE_URL") if platform_raw else url

    connect_args = {"application_name": APPLICATION_NAME, "connect_timeout": 5}
    engine = sa.create_engine(url, pool_pre_ping=True, hide_parameters=True, connect_args=connect_args)
    platform_engine = sa.create_engine(
        platform_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=connect_args,
        pool_size=2,
        max_overflow=2,
    )
    return DatabaseRuntime(engine=engine, platform_engine=platform_engine)


def runtime_role_violations(*, bypasses_rls: bool, superuser: bool) -> list[str]:
    """Decide runtime-role posture from one observed (bypass, super) pair."""

    problems: list[str] = []
    if superuser:
        problems.append(
            "runtime database role is a superuser; a superuser bypasses row-level security regardless of rolbypassrls"
        )
    if bypasses_rls:
        problems.append(
            "runtime database role has BYPASSRLS; an online role that "
            "bypasses RLS defeats every composed module's tenant isolation"
        )
    return problems


def verify_runtime_role_posture(engine: sa.Engine) -> list[str]:
    """Connect once and prove the DSN's own role cannot bypass RLS."""

    try:
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT rolbypassrls, rolsuper, rolcreaterole, rolreplication "
                    "FROM pg_catalog.pg_roles WHERE rolname = current_user"
                )
            ).one_or_none()
            if row is None:
                return ["runtime database role not found in pg_roles"]
            problems = runtime_role_violations(bypasses_rls=bool(row[0]), superuser=bool(row[1]))
            if row[2]:
                problems.append("runtime database role has CREATEROLE")
            if row[3]:
                problems.append("runtime database role has REPLICATION")
            # A member of a BYPASSRLS/superuser role can SET ROLE into it;
            # the pair check alone would call that posture safe.
            inherited = connection.execute(
                sa.text(
                    "WITH RECURSIVE membership(roleid) AS ("
                    " SELECT roleid FROM pg_catalog.pg_auth_members"
                    " WHERE member = (SELECT oid FROM pg_catalog.pg_roles"
                    "                 WHERE rolname = current_user)"
                    " UNION"
                    " SELECT am.roleid FROM pg_catalog.pg_auth_members am"
                    " JOIN membership m ON am.member = m.roleid)"
                    " SELECT count(*) FROM pg_catalog.pg_roles r"
                    " JOIN membership m ON r.oid = m.roleid"
                    " WHERE r.rolbypassrls OR r.rolsuper"
                )
            ).scalar()
            if inherited:
                problems.append("runtime database role inherits BYPASSRLS/superuser through role membership")
            return problems
    except sa.exc.SQLAlchemyError as exc:
        return [f"runtime database posture could not be verified: {type(exc).__name__}"]


def database_startup_checks(
    runtime: DatabaseRuntime | None,
) -> tuple[StartupCheck, ...]:
    """Startup checks for the assembly spec (empty when no runtime bound)."""

    if runtime is None:
        return ()

    def _posture() -> Sequence[str]:
        problems = verify_runtime_role_posture(runtime.engine)
        problems += [f"platform: {problem}" for problem in verify_runtime_role_posture(runtime.platform_engine)]
        return problems

    return (_posture,)


def enforce_runtime_posture(runtime: DatabaseRuntime | None) -> None:
    """Fail closed regardless of ENVIRONMENT.

    Kernel startup checks only raise in production and otherwise log a
    warning (witnessed: a superuser candidate boot served traffic). The
    process entrypoint calls this before serving so a bypass-capable
    runtime DSN can never serve in any environment.
    """

    if runtime is None:
        return
    problems = verify_runtime_role_posture(runtime.engine)
    problems += verify_runtime_role_posture(runtime.platform_engine)
    if problems:
        raise DatabaseBindingError("; ".join(problems))


__all__ = [
    "APPLICATION_NAME",
    "DatabaseBindingError",
    "build_database_runtime",
    "database_startup_checks",
    "enforce_runtime_posture",
    "runtime_role_violations",
    "verify_runtime_role_posture",
]
