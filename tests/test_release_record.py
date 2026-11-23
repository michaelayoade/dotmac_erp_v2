"""scripts/release_record.py: one image per version, exact descriptor and spec bytes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_record.py"
DESCRIPTOR = ROOT / "deploy" / "deploy-unit.toml"
SPEC = ROOT / "deploy" / "product.toml"
DIGEST = "sha256:" + "ab" * 32
IMAGE = "registry.example.invalid/app@" + DIGEST
REVISION = "c" * 40


def _run(
    out: Path, *extra: str, descriptor: Path = DESCRIPTOR, spec: Path | None = SPEC
) -> subprocess.CompletedProcess[str]:
    args = [
        sys.executable,
        "-I",
        str(SCRIPT),
        "--revision",
        REVISION,
        "--version",
        "0.1.0",
        "--descriptor",
        str(descriptor),
        "--descriptor-name",
        "deploy/deploy-unit.toml",
        "--out",
        str(out),
    ]
    if spec is not None and True:
        args += ["--product-toml", str(spec)]
    return subprocess.run(args + list(extra), capture_output=True, text=True, check=False)  # noqa: S603


def test_record_carries_digest_revision_version_and_exact_bytes(tmp_path: Path) -> None:
    done = _run(tmp_path / "out", "--image", IMAGE)
    assert done.returncode == 0, done.stderr
    record = json.loads((tmp_path / "out" / "release-record.json").read_text())
    raw = DESCRIPTOR.read_bytes()
    assert (tmp_path / "out" / "deploy-unit.toml").read_bytes() == raw
    assert record["deploy_unit_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert record["image_ref"] == IMAGE
    assert record["image_digest"] == DIGEST
    assert record["source_revision"] == REVISION
    assert record["version"] == "0.1.0"


def test_exactly_one_image_per_version(tmp_path: Path) -> None:
    other = "registry.example.invalid/app@sha256:" + "cd" * 32
    two = _run(tmp_path / "out", "--image", IMAGE, "--image", other)
    assert two.returncode == 2 and "exactly one" in two.stderr
    assert not (tmp_path / "out").exists()


def test_refuses_sentinel_tag_and_short_revision(tmp_path: Path) -> None:
    zero = "registry.example.invalid/app@sha256:" + "0" * 64
    assert _run(tmp_path / "a", "--image", zero).returncode == 2
    tag = "registry.example.invalid/app:latest"
    assert _run(tmp_path / "b", "--image", tag).returncode == 2
    bad = _run(tmp_path / "c", "--image", IMAGE, "--revision", "abc")
    assert bad.returncode == 2


def test_workflow_uploads_the_record_without_new_triggers() -> None:
    text = (ROOT / ".github" / "workflows" / "image.yml").read_text()
    assert "release_record.py" in text and "release-record" in text
    assert "workflow_dispatch:" in text
    for trigger in ("\n  push:", "\n  pull_request", "\n  schedule:"):
        assert trigger not in text
    block = text.split("Produce release record")[1]
    assert "secrets." not in block.split("upload-artifact")[0]


def test_record_carries_product_toml_path_and_sha(tmp_path: Path) -> None:
    done = _run(tmp_path / "out", "--image", IMAGE)
    assert done.returncode == 0, done.stderr
    record = json.loads((tmp_path / "out" / "release-record.json").read_text())
    raw = SPEC.read_bytes()
    assert record["product_toml_path"] == "deploy/product.toml"
    assert record["product_toml_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert record["product_toml_pending"] is False
    assert (tmp_path / "out" / "product.toml").read_bytes() == raw


def test_sha_is_over_the_bytes_given_not_the_repo_copy(tmp_path: Path) -> None:
    bound = tmp_path / "product.toml"
    bound.write_bytes(SPEC.read_bytes() + b"# CI-bound copy\n")
    done = _run(tmp_path / "out", "--image", IMAGE, spec=bound)
    assert done.returncode == 0, done.stderr
    record = json.loads((tmp_path / "out" / "release-record.json").read_text())
    assert record["product_toml_sha256"] == ("sha256:" + hashlib.sha256(bound.read_bytes()).hexdigest())
    assert record["product_toml_sha256"] != ("sha256:" + hashlib.sha256(SPEC.read_bytes()).hexdigest())


def test_refuses_without_product_toml_and_pending_is_not_allowed(tmp_path: Path) -> None:
    missing = _run(tmp_path / "a", "--image", IMAGE, spec=None)
    assert missing.returncode == 2 and "product.toml" in missing.stderr
    pending = _run(tmp_path / "b", "--image", IMAGE, "--spec-pending", spec=None)
    assert pending.returncode == 2
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()
