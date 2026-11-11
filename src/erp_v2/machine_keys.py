"""Verification secret for kernel machine credentials (held at boot).

The kernel verifies `X-Api-Key` against a dedicated, held secret named
`machine_credential_hmac_key` and has no fallback: with none installed no
key authenticates. The product installs it once at startup (ADR-0009: held,
never dereferenced on the request path). This product reads it from the
`MACHINE_CREDENTIAL_HMAC_KEY` runtime material, supplied by the deployment
secret path and never committed.

Unset is a valid, fail-closed state: nothing is installed and every
machine-authenticated route answers `not_configured`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from dotmac_kernel.machine_auth import MACHINE_KEY_SECRET_NAME
from dotmac_kernel.secret_sources import install_secret_source

MACHINE_KEY_ENV: Final = "MACHINE_CREDENTIAL_HMAC_KEY"


class _EnvMachineKeySource:
    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def load(self) -> Mapping[str, str]:
        value = self._environ.get(MACHINE_KEY_ENV, "")
        return {MACHINE_KEY_SECRET_NAME: value} if value else {}


def install_machine_key_secret(environ: Mapping[str, str] | None = None) -> bool:
    """Install the verification secret from the environment. Returns whether
    one was installed (never the value)."""

    env = os.environ if environ is None else environ
    names = install_secret_source(_EnvMachineKeySource(env))
    return MACHINE_KEY_SECRET_NAME in names


__all__ = ["MACHINE_KEY_ENV", "install_machine_key_secret"]
