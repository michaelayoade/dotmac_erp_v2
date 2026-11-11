"""Product-owned process launcher for the composed ERP shell.

The deployment descriptor names this launcher and the container port. The
bind address stays here, in code bound by the image digest, rather than in a
deployment document.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import uvicorn
from dotmac_kernel.secret_sources import SecretError

from erp_v2.assembly import database_runtime
from erp_v2.database import DatabaseBindingError, enforce_runtime_posture
from erp_v2.machine_keys import install_machine_key_secret
from erp_v2.main import app
from erp_v2.materials import RuntimeMaterialsError, enforce_runtime_materials


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.runtime")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("APP_PORT", "8000")),
        help="Container port to bind (default: APP_PORT or 8000)",
    )
    args = parser.parse_args(argv)
    # Fail closed in every environment: kernel startup checks only warn
    # outside production, which once let a superuser DSN serve traffic.
    try:
        enforce_runtime_materials()
        enforce_runtime_posture(database_runtime)
        # Held verification secret for kernel machine credentials. Unset is
        # fail-closed: machine-authenticated routes answer not_configured.
        install_machine_key_secret()
    except (DatabaseBindingError, RuntimeMaterialsError, SecretError) as exc:
        print(f"refusing to serve: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    uvicorn.run(app, host="0.0.0.0", port=args.port)  # noqa: S104 - container bind, by design


if __name__ == "__main__":
    main()
