"""Runtime secret-materials gate (security review M2).

The kernel enforces its secret rules only when ``ENVIRONMENT`` is
production; every other environment serves with the published sentinel
secrets (``dev-insecure-change-me``) and ``seed_on_startup`` defaulting
on. A candidate is exactly such a non-production process, so the product
entrypoint enforces the production-grade rules itself, in every
environment, once a database is bound:

- JWT/SESSION/CSRF secrets present, non-sentinel, >= 32 bytes each;
- CSRF secret distinct from the other two (kernel production rule);
- ``MACHINE_CREDENTIAL_HMAC_KEY``, when supplied, >= 32 bytes and distinct
  (optional: unset keeps machine-authenticated routes closed);
- ``ANONYMOUS_RATE_LIMIT_KEY_SECRET``, whenever anonymous routes are
  declared and the store is not memory (default postgres; checked even
  without a bound DATABASE_URL): present, non-sentinel, >= 32 bytes and
  distinct from the other secrets (the kernel checks it in production only);
- every lookup uses a canonical-name view of the env (matched with
  ``str.lower()`` like the kernel, whose settings are
  case-insensitive); two spellings of one gate key are refused;
- ``TRUSTED_HOSTS`` declared;
- ``SEED_ON_STARTUP`` explicitly false (absent means the kernel default,
  which is on -- a serving process must never seed).

Empty-shell mode (no ``DATABASE_URL``) needs none of this: it holds no
sessions, signs nothing, and cannot seed. Problem messages name the
material only; they never echo a value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

_KERNEL_DEV_SENTINEL: Final = "dev-insecure-change-me"
_MIN_SECRET_BYTES: Final = 32
_SECRET_NAMES: Final = ("JWT_SECRET", "SESSION_HASH_SECRET", "CSRF_SECRET")
_ANON_KEY_NAME: Final = "ANONYMOUS_RATE_LIMIT_KEY_SECRET"
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_FALSY: Final = frozenset({"0", "false", "no", "off"})


_GATE_KEYS: Final = frozenset(
    (
        "JWT_SECRET",
        "SESSION_HASH_SECRET",
        "CSRF_SECRET",
        "ANONYMOUS_RATE_LIMIT_ROUTES",
        "ANONYMOUS_RATE_LIMIT_STORE",
        "ANONYMOUS_RATE_LIMIT_KEY_SECRET",
        "DATABASE_URL",
        "TRUSTED_HOSTS",
        "SEED_ON_STARTUP",
        "MACHINE_CREDENTIAL_HMAC_KEY",
    )
)


_GATE_LOWER: Final = {name.lower(): name for name in _GATE_KEYS}


def _gate_view(env: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Canonical-name view of the gate keys, plus keys set under >1 spelling.

    The kernel (pydantic-settings) matches a name when ``key.lower()`` equals
    the lower-case field name, so match the same way: ``str.upper()`` disagrees
    with ``str.lower()`` on some non-ASCII characters.
    """

    view: dict[str, str] = {}
    counts: dict[str, int] = {}
    for key, value in env.items():
        canonical = _GATE_LOWER.get(key.lower())
        if canonical is None:
            continue
        view[canonical] = value
        counts[canonical] = counts.get(canonical, 0) + 1
    return view, sorted(name for name, count in counts.items() if count > 1)


class RuntimeMaterialsError(RuntimeError):
    """Declared runtime materials are unfit to serve. Sanitized: no values."""


def _anonymous_key_problems(env: Mapping[str, str]) -> list[str]:
    """Key rule for declared anonymous routes (kernel: production only)."""

    if not env.get("ANONYMOUS_RATE_LIMIT_ROUTES", "").strip():
        return []
    # Kernel default store is postgres; any other non-memory value is refused
    # by the kernel at startup, so failing closed here is consistent.
    store = env.get("ANONYMOUS_RATE_LIMIT_STORE", "postgres").strip().lower()
    if store == "memory":
        return []
    anon_key = env.get(_ANON_KEY_NAME, "")
    if not anon_key:
        return [f"{_ANON_KEY_NAME} must be supplied when anonymous routes use the postgres store"]
    problems: list[str] = []
    if anon_key == _KERNEL_DEV_SENTINEL:
        problems.append(f"{_ANON_KEY_NAME} is the kernel's published dev sentinel; supply a deployment secret")
    elif len(anon_key.encode("utf-8")) < _MIN_SECRET_BYTES:
        problems.append(f"{_ANON_KEY_NAME} must contain at least 32 bytes")
    if anon_key in {env.get(name, "") for name in (*_SECRET_NAMES, "MACHINE_CREDENTIAL_HMAC_KEY")}:
        problems.append(f"{_ANON_KEY_NAME} must be distinct from the other secrets")
    return problems


def runtime_materials_problems(
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """List material defects for a DB-bound runtime process (empty = fit)."""

    raw = os.environ if environ is None else environ
    # The kernel's BaseSettings is case-insensitive, so a lower-case name
    # configures it while an exact-name lookup sees nothing. Read through a
    # lower-case canonical-name view (see _gate_view); two spellings of one
    # gate key are ambiguous: refuse.
    env, collisions = _gate_view(raw)
    if collisions:
        return [f"{name} is set under more than one letter case; supply exactly one spelling" for name in collisions]
    if not env.get("DATABASE_URL", "").strip():
        # The anonymous store counts through PLATFORM_DATABASE_URL, so its key
        # rule holds even without a bound DATABASE_URL.
        return _anonymous_key_problems(env)
    problems: list[str] = []
    secrets: dict[str, str] = {}
    for name in _SECRET_NAMES:
        value = env.get(name, "")
        if not value:
            problems.append(f"{name} must be supplied when a database is bound")
            continue
        secrets[name] = value
        if value == _KERNEL_DEV_SENTINEL:
            problems.append(f"{name} is the kernel's published dev sentinel; supply a deployment secret")
        elif len(value.encode("utf-8")) < _MIN_SECRET_BYTES:
            problems.append(f"{name} must contain at least 32 bytes")
    csrf = secrets.get("CSRF_SECRET")
    if csrf and csrf in {
        secrets.get("JWT_SECRET"),
        secrets.get("SESSION_HASH_SECRET"),
    }:
        problems.append("CSRF_SECRET must be distinct from JWT_SECRET and SESSION_HASH_SECRET")
    machine_key = env.get("MACHINE_CREDENTIAL_HMAC_KEY", "")
    if machine_key:
        # Optional (unset keeps machine routes closed), but never weak or reused.
        if len(machine_key.encode("utf-8")) < _MIN_SECRET_BYTES:
            problems.append("MACHINE_CREDENTIAL_HMAC_KEY must contain at least 32 bytes")
        if machine_key in secrets.values():
            problems.append("MACHINE_CREDENTIAL_HMAC_KEY must be distinct from the session and signing secrets")
    problems.extend(_anonymous_key_problems(env))
    if not env.get("TRUSTED_HOSTS", "").strip():
        problems.append("TRUSTED_HOSTS must be declared when a database is bound")
    seed_raw = env.get("SEED_ON_STARTUP")
    if seed_raw is None:
        problems.append("SEED_ON_STARTUP must be explicitly false when a database is bound (kernel default is on)")
    elif seed_raw.strip().lower() in _TRUTHY:
        problems.append("SEED_ON_STARTUP must be false when a database is bound; a serving process never seeds")
    elif seed_raw.strip().lower() not in _FALSY:
        problems.append("SEED_ON_STARTUP is not a recognizable boolean")
    return problems


def enforce_runtime_materials(environ: Mapping[str, str] | None = None) -> None:
    """Fail closed before serving when runtime materials are unfit."""

    problems = runtime_materials_problems(environ)
    if problems:
        raise RuntimeMaterialsError("; ".join(problems))


__all__ = [
    "RuntimeMaterialsError",
    "enforce_runtime_materials",
    "runtime_materials_problems",
]
