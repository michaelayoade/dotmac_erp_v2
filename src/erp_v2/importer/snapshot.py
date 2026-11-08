"""Snapshot binding: an import is bound to exact source bytes or refused.

A manifest names the production snapshot digest, both database
identities, the candidate revision and the explicit tenant extent.
The CLI refuses to plan or execute without one, and the receipt
echoes the binding so a rehearsal/flip record can never drift from
the snapshot it actually ran against (cutover Gate 3/4).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(RuntimeError):
    """The snapshot manifest is missing or malformed."""


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_sha256: str
    source_database: str
    destination_database: str
    candidate_revision: str
    tenant_extent: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> SnapshotManifest:
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot read snapshot manifest {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestError("snapshot manifest must be a JSON object")
        required = (
            "snapshot_sha256",
            "source_database",
            "destination_database",
            "candidate_revision",
            "tenant_extent",
        )
        missing = [k for k in required if k not in raw or raw[k] in (None, "", [])]
        if missing:
            raise ManifestError(f"snapshot manifest missing fields: {missing}")
        if not _SHA256.match(str(raw["snapshot_sha256"])):
            raise ManifestError("snapshot_sha256 must be 64 lowercase hex chars")
        if raw["source_database"] == raw["destination_database"]:
            raise ManifestError("source and destination databases must differ")
        extent = raw["tenant_extent"]
        if not isinstance(extent, list) or not all(isinstance(x, str) and x for x in extent):
            raise ManifestError("tenant_extent must be a non-empty list of tenant ids")
        return cls(
            snapshot_sha256=str(raw["snapshot_sha256"]),
            source_database=str(raw["source_database"]),
            destination_database=str(raw["destination_database"]),
            candidate_revision=str(raw["candidate_revision"]),
            tenant_extent=tuple(extent),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "snapshot_sha256": self.snapshot_sha256,
            "source_database": self.source_database,
            "destination_database": self.destination_database,
            "candidate_revision": self.candidate_revision,
            "tenant_extent": list(self.tenant_extent),
        }


__all__ = ["ManifestError", "SnapshotManifest"]
