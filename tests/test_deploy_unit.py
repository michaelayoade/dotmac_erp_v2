"""deploy/deploy-unit.toml against the dotmac.deploy_unit/1 contract (design rev3 sections 2.2, 2.7, 2.8).

Static checks on the committed descriptor and its sidecar spec. They read two
TOML files and one source file as text: no database, no network, no import of
the application. The Foundation read-back (`validate`) runs only where the
Foundation distribution is installed.

The contract checker below is written against the design document, not against
Control a18 (not yet built); it is deliberately small and refuses by name.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy" / "deploy-unit.toml"
SPEC_PATH = ROOT / "deploy" / "product.toml"

SCHEMA = "dotmac.deploy_unit/1"
NAMESPACE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
EXPECTED_NAMESPACE = "erp"
PARAM_TYPES = frozenset(
    {
        "string",
        "int",
        "bool",
        "enum",
        "hostname",
        "cidr",
        "url",
        "duration_seconds",
        "path",
        "image_digest",
        "image_repository",
    }
)
WRITERS = frozenset({"generated", "operator", "ceremony", "integration"})
ROTATIONS = frozenset({"copy_only", "manual"})
HEALTH_GATES = frozenset(
    {
        "migrate",
        "grant_mirror",
        "heads",
        "live",
        "ready",
        "composition_equals_pins",
        "runtime_policy_digest",
        "census_zero_enabled",
    }
)
BINDING_STATES = frozenset({"pre_staged", "active"})
# H2: file arguments of a job are restricted to that job's own work dir, and the
# importer's env-name overrides can never be passed.
JOB_IN, JOB_OUT = "/job/in", "/job/out"
NEVER_FLAGS = frozenset({"--source-url-env", "--dest-url-env"})
# Import-only OpenBao: these belong to a DISPATCHED job and to nothing else. They
# are never a legacy_env_file name and never a material of the serving app (an
# unscoped param); they reach the job only through its role's mint.
JOB_ONLY_MATERIALS = frozenset({"MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL"})
MATERIAL_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
# Control is default-deny: legacy_env_file_allowed_targets is empty until Michael
# enables a target (ERP v2 is the only intended one), and a descriptor under
# deploy/companion/ is always refused.
LEGACY_ALLOWED_DEFAULT = frozenset({"dotmac-erp-v2"})
REFUSED_BY_DEFAULT = frozenset({"dotmac-sub", "dotmac-integrator-companion"})
SECRET_NAME_HINT = re.compile(r"(SECRET|TOKEN|PASSWORD|KEY|DATABASE_URL|_DSN)$")
# Values a params file could never carry: secret-shaped content and estate addresses.
SECRET_SHAPED = re.compile(
    r"(-----BEGIN|bao://|hvs\.|s\.[A-Za-z0-9]{20,}|[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^@\s]+@)"
)
IPV4_LITERAL = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def _load(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _walk_strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _walk_strings(value)]
    if isinstance(node, list):
        return [s for value in node for s in _walk_strings(value)]
    return []


def _bad_value(value: str) -> bool:
    """The grammar Foundation's env parser would otherwise alter (F1), plus secret shapes."""

    if "\n" in value or "\r" in value or "\x00" in value:
        return True
    if value != value.strip():
        return True
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return True
    return bool(SECRET_SHAPED.search(value))


def check_unit(
    unit: dict[str, Any],
    spec: dict[str, Any],
    *,
    companion_path: bool = False,
) -> list[str]:
    """Return refusal codes for a descriptor; empty means it satisfies the contract."""

    problems: list[str] = []
    if unit.get("schema") != SCHEMA:
        problems.append("schema")
    product = unit.get("product", "")
    if not product:
        problems.append("product")
    namespace = unit.get("secrets_namespace")
    if not isinstance(namespace, str) or NAMESPACE.fullmatch(namespace) is None:
        problems.append("secrets-namespace")
    if unit.get("spec") != "deploy/product.toml" and not companion_path:
        problems.append("spec-path")
    if "spec_digest" in unit:
        # Digests are bound by the release record, never stored beside a sentinel image.
        problems.append("spec-digest-in-descriptor")

    # [image]: absent means transfer; pull is never self-declared (section 2.7).
    source = unit.get("image", {}).get("source", "transfer")
    if source not in {"transfer", "pull"}:
        problems.append("image-source")
    if source == "pull":
        problems.append("image-pull-requires-allowlist")

    materials = set(spec.get("runtime_materials", {}).get("names", []))
    params = unit.get("params", {})
    seen_materials: dict[str, str] = {}
    for name, param in params.items():
        if param.get("type") not in PARAM_TYPES:
            problems.append(f"param-type:{name}")
        material = param.get("material")
        if material is not None:
            if not MATERIAL_NAME.match(material):
                problems.append(f"param-material-name:{name}")
            scoped = param.get("jobs", [])
            # A job-scoped param reaches only its job's env, never Foundation, so
            # it is not a product.toml runtime material.
            if materials and material not in materials and not scoped:
                problems.append(f"param-material-unknown:{name}")
            if material in JOB_ONLY_MATERIALS and not scoped:
                problems.append(f"param-job-only-material:{name}")
            if material in seen_materials:
                problems.append(f"param-material-duplicate:{name}")
            seen_materials[material] = name
            if SECRET_NAME_HINT.search(material):
                problems.append(f"param-material-secret-class:{name}")
        scope = param.get("jobs", [])
        if scope and (not isinstance(scope, list) or not set(scope) <= {j.get("name") for j in unit.get("jobs", [])}):
            problems.append(f"param-job-unknown:{name}")
        if scope and material is None:
            problems.append(f"param-job-material:{name}")
        for key in ("default", "pattern"):
            value = param.get(key)
            if isinstance(value, str) and key == "default" and _bad_value(value):
                problems.append(f"param-value:{name}")
        if param.get("type") == "duration_seconds" and "max" not in param:
            problems.append(f"param-unbounded:{name}")

    bindings = unit.get("bindings", [])
    binding_names: set[str] = set()
    active_binding_names: set[str] = set()
    for binding in bindings:
        name = binding.get("name", "")
        if not MATERIAL_NAME.match(name):
            problems.append(f"binding-name:{name}")
        if name in binding_names:
            problems.append(f"binding-duplicate:{name}")
        binding_names.add(name)
        if binding.get("state", "active") not in BINDING_STATES:
            problems.append(f"binding-state:{name}")
        elif binding.get("state", "active") == "active":
            active_binding_names.add(name)
        if binding.get("writer") not in WRITERS:
            problems.append(f"binding-writer:{name}")
        if binding.get("rotation") not in ROTATIONS:
            problems.append(f"binding-rotation:{name}")
        if not binding.get("roles") or not binding.get("kind"):
            problems.append(f"binding-incomplete:{name}")
        if "single_use" not in binding or not isinstance(binding["single_use"], bool):
            problems.append(f"binding-single-use:{name}")
        # A binding is a pointer: it never carries a value or a reference literal.
        if {"value", "secret_ref", "default"} & binding.keys():
            problems.append(f"binding-carries-value:{name}")
        if name in seen_materials:
            problems.append(f"binding-collides-with-param:{name}")

    legacy = unit.get("legacy_env_file")
    if legacy is not None:
        if product not in LEGACY_ALLOWED_DEFAULT or companion_path:
            problems.append("legacy-refused")
        names = legacy.get("names", [])
        if not names or len(set(names)) != len(names):
            problems.append("legacy-names")
        for name in names:
            if not MATERIAL_NAME.match(name):
                problems.append(f"legacy-name-shape:{name}")
            if not SECRET_NAME_HINT.search(name):
                problems.append(f"legacy-nonsecret-name:{name}")
            if name in seen_materials or name == "APP_IMAGE":
                problems.append(f"legacy-collides-with-param:{name}")
            if name in active_binding_names:
                problems.append(f"legacy-collides-with-binding:{name}")
            if name in JOB_ONLY_MATERIALS:
                problems.append(f"legacy-job-credential:{name}")
        if set(legacy) - {"names", "path_param"}:
            problems.append("legacy-extra-keys")
        path_param = params.get(legacy.get("path_param", ""), {})
        if path_param.get("type") != "path" or "material" in path_param or "default" in path_param:
            problems.append("legacy-path-param")

    for companion in unit.get("companions", []):
        if companion.get("order") != "after_primary_ready":
            problems.append("companion-order")
        if not isinstance(companion.get("required_at_switch"), bool):
            problems.append("companion-required-at-switch")
        if companion.get("host") not in {"same", "param"}:
            problems.append("companion-host")
        if not companion.get("product") or not companion.get("descriptor"):
            problems.append("companion-incomplete")

    for hold in unit.get("hold_points", []):
        timeout = params.get(hold.get("timeout_param", ""), {})
        if timeout.get("type") != "duration_seconds" or timeout.get("max", 10**9) > 3600:
            problems.append(f"hold-point-unbounded:{hold.get('name')}")

    for name, param in params.items():
        if ("min" in param or "max" in param) and param.get("type") not in {
            "int",
            "duration_seconds",
        }:
            problems.append(f"param-bounds-type:{name}")
    binding_roles = {r for b in bindings for r in b.get("roles", [])}
    for job in unit.get("jobs", []):
        if job.get("trigger") != "operator":
            problems.append(f"job-trigger:{job.get('name')}")
        if job.get("role") is not None and job["role"] not in binding_roles:
            problems.append(f"job-role-unknown:{job.get('name')}")
        timeout = job.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3480:
            problems.append(f"job-timeout:{job.get('name')}")
        flags = [a.get("flag") for a in job.get("args", [])]
        if len(set(flags)) != len(flags):
            problems.append(f"job-arg-twice:{job.get('name')}")
        for arg in job.get("args", []):
            flag = str(arg.get("flag"))
            if not re.fullmatch(r"--[a-z][a-z0-9-]*", flag) or flag in NEVER_FLAGS:
                problems.append(f"job-arg-flag:{job.get('name')}:{flag}")
            if arg.get("type") not in {"string", "path", "enum", "bool", "int", "sha256"}:
                problems.append(f"job-arg-type:{job.get('name')}:{flag}")
            if arg.get("type") == "path":
                root = {"input": JOB_IN, "output": JOB_OUT}.get(arg.get("io"))
                if root is None or not str(arg.get("pattern", "")).startswith(f"^{root}/"):
                    problems.append(f"job-arg-path-outside-workdir:{job.get('name')}:{flag}")
        known = {p["material"] for p in params.values() if "material" in p}
        for material in job.get("materials", []):
            if material not in known | binding_names:
                problems.append(f"job-material-unknown:{material}")
        runbook = job.get("runbook", "")
        if runbook.startswith("/") or ".." in runbook.split("/"):
            problems.append(f"job-runbook-path:{job.get('name')}")
    for hold in unit.get("hold_points", []):
        if hold.get("member") not in {"primary", "companion"}:
            problems.append("hold-member")
        if hold.get("before") not in {"migrate", "start", "switch"}:
            problems.append("hold-before")
        if hold.get("on_timeout") != "abandon_member_record_deviation":
            problems.append("hold-on-timeout")
        if hold.get("released_by") != "operator":
            problems.append("hold-released-by")
    health = unit.get("health")
    if health is not None:
        gates = health.get("gates", [])
        if set(health) != {"gates"} or len(set(gates)) != len(gates):
            problems.append("health-shape")
        if not set(gates) <= HEALTH_GATES:
            problems.append("health-gate-unknown")
    for text in _walk_strings(unit):
        if SECRET_SHAPED.search(text):
            problems.append("secret-shaped-string")
        if IPV4_LITERAL.search(text):
            problems.append("ip-literal")
    return problems


def _unit() -> dict[str, Any]:
    return _load(UNIT_PATH)


def _spec() -> dict[str, Any]:
    return _load(SPEC_PATH)


def _check(unit: dict[str, Any]) -> list[str]:
    return check_unit(unit, _spec())


def test_descriptor_satisfies_the_contract() -> None:
    assert _check(_unit()) == []


def test_spec_is_the_sidecar_product_toml() -> None:
    unit = _unit()
    assert unit["spec"] == "deploy/product.toml"
    assert (ROOT / unit["spec"]).is_file()
    assert unit["product"] == _spec()["product"]
    stored = copy.deepcopy(unit)
    stored["spec_digest"] = "sha256:" + "1" * 64
    assert "spec-digest-in-descriptor" in _check(stored)
    moved = copy.deepcopy(unit)
    moved["spec"] = "deploy/other.toml"
    assert "spec-path" in _check(moved)


def test_image_source_default_transfer() -> None:
    unit = _unit()
    assert unit["image"]["source"] == "transfer"
    # An absent [image] table means transfer, not pull.
    absent = copy.deepcopy(unit)
    del absent["image"]
    assert _check(absent) == []
    # A descriptor cannot widen this by itself.
    pulled = copy.deepcopy(unit)
    pulled["image"]["source"] = "pull"
    assert "image-pull-requires-allowlist" in _check(pulled)
    other = copy.deepcopy(unit)
    other["image"]["source"] = "registry"
    assert "image-source" in _check(other)


def test_legacy_never_products() -> None:
    # ERP v2 is an existing app: the bridge is allowed here ...
    unit = _unit()
    assert "legacy_env_file" in unit
    assert unit["product"] in LEGACY_ALLOWED_DEFAULT
    assert unit["product"] not in REFUSED_BY_DEFAULT
    # ... but is refused for sub_thin, for the companion, and for any companion path.
    for product in sorted(REFUSED_BY_DEFAULT):
        refused = copy.deepcopy(unit)
        refused["product"] = product
        assert "legacy-refused" in _check(refused)
    companion = copy.deepcopy(unit)
    assert "legacy-refused" in check_unit(companion, _spec(), companion_path=True)


def test_legacy_env_file_is_names_only_and_secret_class() -> None:
    unit = _unit()
    legacy = unit["legacy_env_file"]
    assert set(legacy) == {"names", "path_param"}
    assert len(set(legacy["names"])) == len(legacy["names"])
    for name in legacy["names"]:
        assert SECRET_NAME_HINT.search(name), name
    path_param = unit["params"][legacy["path_param"]]
    assert path_param["type"] == "path"
    assert "default" not in path_param and "material" not in path_param


def test_legacy_names_do_not_collide() -> None:
    unit = _unit()
    names = set(unit["legacy_env_file"]["names"])
    assert names.isdisjoint({p["material"] for p in unit["params"].values() if "material" in p})
    assert "APP_IMAGE" not in names
    for name in sorted(names):
        clash = copy.deepcopy(unit)
        clash["params"]["clash"] = {"type": "string", "material": name}
        assert any(p.startswith("legacy-collides-with-param") for p in _check(clash)), name
    extra = copy.deepcopy(unit)
    extra["legacy_env_file"]["names"] = [*names, "PLATFORM_ROOT_DOMAIN"]
    assert any(p.startswith("legacy-nonsecret-name") for p in _check(extra))
    keyed = copy.deepcopy(unit)
    keyed["legacy_env_file"]["names"] = [*names, "APP_IMAGE"]
    assert any(p.startswith("legacy-collides-with-param") for p in _check(keyed))


def test_legacy_names_are_secret_names_in_product_materials() -> None:
    materials = set(_spec()["runtime_materials"]["names"])
    assert set(_unit()["legacy_env_file"]["names"]) <= materials


def test_pre_staged_bindings_are_not_active_sources() -> None:
    unit = _unit()
    assert unit["bindings"], "the exit from the bridge is pre-staged"
    assert all(b["state"] == "pre_staged" for b in unit["bindings"])
    # Once a binding is active it may not also be a legacy name (double source).
    active = copy.deepcopy(unit)
    active["bindings"][0]["state"] = "active"
    name = active["bindings"][0]["name"]
    if name in unit["legacy_env_file"]["names"]:
        assert f"legacy-collides-with-binding:{name}" in _check(active)


def test_bindings_are_pointers_never_values() -> None:
    unit = _unit()
    for binding in unit["bindings"]:
        assert {"name", "kind", "roles", "writer", "rotation", "single_use"} <= binding.keys()
        assert not {"value", "secret_ref", "default"} & binding.keys()
    leaky = copy.deepcopy(unit)
    leaky["bindings"][0]["secret_ref"] = "bao://production/dotmac-erp-v2/kv/app/x"  # noqa: S105 - fixture, a pointer
    assert "binding-carries-value:" + leaky["bindings"][0]["name"] in _check(leaky)
    # The migrate owner credential is single-use; serving credentials are not.
    by_name = {b["name"]: b for b in unit["bindings"]}
    assert by_name["MIGRATION_DATABASE_URL"]["single_use"] is True
    assert by_name["DATABASE_URL"]["single_use"] is False
    assert "migrate" not in by_name["DATABASE_URL"]["roles"]


def test_params_are_typed_and_refuse_unsafe_values() -> None:
    unit = _unit()
    assert {p["type"] for p in unit["params"].values()} <= PARAM_TYPES
    for value in ("a\nb", "nul\x00", " lead", "trail ", '"quoted"', "'quoted'", "bao://x/y", "-----BEGIN KEY"):
        bad = copy.deepcopy(unit)
        bad["params"]["platform_root_domain"]["default"] = value
        assert "param-value:platform_root_domain" in _check(bad), repr(value)
    unknown = copy.deepcopy(unit)
    unknown["params"]["platform_root_domain"]["type"] = "secret"
    assert "param-type:platform_root_domain" in _check(unknown)
    secretish = copy.deepcopy(unit)
    secretish["params"]["platform_root_domain"]["material"] = "JWT_SECRET"
    assert "param-material-secret-class:platform_root_domain" in _check(secretish)


def test_param_materials_exist_in_product_spec() -> None:
    materials = set(_spec()["runtime_materials"]["names"])
    for name, param in _unit()["params"].items():
        # Job-scoped params reach only their job's env, never Foundation.
        if "material" in param and not param.get("jobs"):
            assert param["material"] in materials, name


def test_serving_role_never_receives_owner_or_backup_material() -> None:
    spec = _spec()
    (app,) = [r for r in spec["roles"] if r["code"] == "app"]
    assert "MIGRATION_DATABASE_URL" not in app["materials"]
    assert "BACKUP_DATABASE_URL" not in app["materials"]
    assert spec["migration"]["owner_material"] == "MIGRATION_DATABASE_URL"


def test_companion_reference_after_primary_ready() -> None:
    (companion,) = _unit()["companions"]
    assert companion["order"] == "after_primary_ready"
    assert companion["required_at_switch"] is False
    assert companion["descriptor"] == "deploy/companion/deploy-unit.toml"
    # The companion is a separate product: it must not be ERP's own descriptor.
    assert companion["product"] != _unit()["product"]
    broken = copy.deepcopy(_unit())
    broken["companions"][0]["order"] = "before_primary"
    assert "companion-order" in _check(broken)


def test_a5_hold_point_is_bounded() -> None:
    unit = _unit()
    (hold,) = unit["hold_points"]
    assert hold["member"] == "companion" and hold["released_by"] == "operator"
    timeout = unit["params"][hold["timeout_param"]]
    assert timeout["type"] == "duration_seconds"
    assert timeout["default"] == 1800 and timeout["max"] <= 3600
    unbounded = copy.deepcopy(unit)
    unbounded["params"][hold["timeout_param"]]["max"] = 86400
    assert f"hold-point-unbounded:{hold['name']}" in _check(unbounded)


def test_jobs_are_operator_triggered_names_only() -> None:
    unit = _unit()
    materials = set(_spec()["runtime_materials"]["names"])
    for job in unit["jobs"]:
        assert job["trigger"] == "operator"
        # Job materials are product materials, or credentials of the job's own
        # role (a binding), which Foundation never sees.
        role_bound = {b["name"] for b in unit["bindings"] if job.get("role") in b["roles"]}
        assert set(job["materials"]) <= materials | role_bound
        assert all(MATERIAL_NAME.match(m) for m in job["materials"])
        assert not any(_bad_value(arg) for arg in job["command"])


def test_no_dotenv_and_no_env_file_anywhere_in_deploy() -> None:
    deploy = ROOT / "deploy"
    assert not [p for p in deploy.rglob("*") if p.name == ".env" or p.name.startswith(".env.")]
    for path in (UNIT_PATH, SPEC_PATH):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*env_file\s*[:=]", text, re.MULTILINE), path.name
    assert not (deploy / "docker-compose.override.yml").exists()


def test_descriptor_holds_no_secret_shaped_value_or_host_address() -> None:
    for path in (UNIT_PATH, SPEC_PATH):
        for text in _walk_strings(_load(path)):
            assert not SECRET_SHAPED.search(text), path.name
            assert not IPV4_LITERAL.search(text), path.name


def test_product_toml_heads_equal_stamped_heads_in_migrate() -> None:
    tree = ast.parse((ROOT / "src" / "erp_v2" / "migrate.py").read_text(encoding="utf-8"))
    consts: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in {"EXPECTED_HEADS", "_ABSORBED_HEADS"} and node.value is not None:
                consts[node.target.id] = tuple(ast.literal_eval(node.value))
    stamped = tuple(h for h in consts["EXPECTED_HEADS"] if h not in consts["_ABSORBED_HEADS"])
    assert sorted(_spec()["migration"]["expected_heads"]) == sorted(stamped)


def test_spec_is_foundation_v3_or_later() -> None:
    assert _spec()["schema"] in {"ProductDeploymentSpec.v3", "ProductDeploymentSpec.v4"}


def test_foundation_validate_accepts_product_toml() -> None:
    # Skipped where the Foundation distribution is not installed; the read-back
    # is also run from a Starter source checkout when the descriptor changes.
    cli = pytest.importorskip("dotmac_deployment_foundation.cli")
    assert cli.main(["-f", str(SPEC_PATH), "validate"]) == 0


def test_secrets_namespace_matches_the_segment_grammar() -> None:
    unit = _unit()
    assert unit["secrets_namespace"] == EXPECTED_NAMESPACE
    assert NAMESPACE.fullmatch(unit["secrets_namespace"])
    for bad in ("", "Erp", "-x", "a/b", "a b", "x" * 65, "a\n", "staging/erp"):
        broken = copy.deepcopy(unit)
        broken["secrets_namespace"] = bad
        assert "secrets-namespace" in _check(broken), repr(bad)
    missing = copy.deepcopy(unit)
    del missing["secrets_namespace"]
    assert "secrets-namespace" in _check(missing)


def test_authored_openbao_config_uses_the_descriptor_namespace() -> None:
    # No app-authored OpenBao config exists under deploy/ yet (D2 / I3), so this
    # holds vacuously until one lands; then every bao:// reference in it must use
    # the descriptor's namespace as its <product> segment.
    namespace = _unit()["secrets_namespace"]
    for path in sorted(ROOT.joinpath("deploy").rglob("*")):
        if path.is_file() and "openbao" in path.parts:
            for match in re.finditer(r"bao://[^/\s]+/([^/\s]+)/", path.read_text()):
                assert match.group(1) == namespace, path.name


# ── Import-only OpenBao (Michael's ruling): the import job's own role ─────────


def test_the_import_job_has_its_own_role_and_two_dsn_materials() -> None:
    unit = _unit()
    (job,) = [j for j in unit["jobs"] if j["name"] == "import_execute"]
    assert job["role"] == "import"
    # The measured importer execute (22m56s, docs/deployment.md) needs >= 1800 s.
    assert job["timeout_seconds"] >= 1800
    # pre_staged delivers nothing: the flip release must set BOTH to "active".
    assert {b["state"] for b in unit["bindings"] if "import" in b["roles"]} == {"pre_staged"}
    assert job["materials"] == ["MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL"]
    by_name = {b["name"]: b for b in unit["bindings"]}
    assert by_name["MIGRATION_DATABASE_URL"]["roles"] == ["migrate", "import"]
    legacy = by_name["LEGACY_DATABASE_URL"]
    assert legacy["state"] == "pre_staged"
    assert legacy["kind"] == "pg_login_dsn"
    assert legacy["roles"] == ["import"]
    assert legacy["writer"] == "ceremony"
    assert legacy["rotation"] == "copy_only"
    # P-SEC rev4: both import bindings are single-use (one secret_id per run).
    assert legacy["single_use"] is True
    assert by_name["MIGRATION_DATABASE_URL"]["single_use"] is True
    # The serving app never gets either credential.
    for name in ("MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL"):
        assert "app" not in by_name[name]["roles"], name
    unknown = copy.deepcopy(unit)
    unknown["jobs"][1]["role"] = "ghost"
    assert f"job-role-unknown:{unknown['jobs'][1]['name']}" in _check(unknown)


def test_job_credentials_are_never_legacy_names_or_app_materials() -> None:
    unit = _unit()
    names = set(unit["legacy_env_file"]["names"])
    assert names.isdisjoint(JOB_ONLY_MATERIALS)
    for name in sorted(JOB_ONLY_MATERIALS):
        leaky = copy.deepcopy(unit)
        leaky["legacy_env_file"]["names"] = [*names, name]
        assert f"legacy-job-credential:{name}" in _check(leaky), name
        app = copy.deepcopy(unit)
        app["params"]["leak"] = {"type": "string", "material": name}
        assert "param-job-only-material:leak" in _check(app), name
        # Scoped to the job it is the job's own, not the app's.
        scoped = copy.deepcopy(unit)
        scoped["params"]["mine"] = {"type": "path", "material": name, "jobs": ["import_execute"]}
        assert not any("param-job-only-material" in p for p in _check(scoped)), name
    # Nor is either a product (Foundation) runtime material of the serving app.
    (app_role,) = [r for r in _spec()["roles"] if r["code"] == "app"]
    assert "LEGACY_DATABASE_URL" not in app_role["materials"]
    assert "LEGACY_DATABASE_URL" not in _spec()["runtime_materials"]["names"]


def test_import_params_are_job_scoped_and_typed_as_the_importer_reads_them() -> None:
    unit = _unit()
    params = unit["params"]
    scoped = {n: p for n, p in params.items() if p.get("jobs")}
    assert {
        "ERP_IMPORT_APPROVED",
        "ERP_IMPORT_APPLY_OK",
        "ERP_ENROLMENT_ACTOR_ID",
        "ERP_PAYROLL_NET_PAYABLE_ACCOUNT_REF",
    } <= {p["material"] for p in scoped.values()}
    assert "import_execute" in params["import_approved"]["jobs"]
    assert "import_execute" in params["import_apply_ok"]["jobs"]
    assert params["enrolment_actor_id"]["jobs"] == ["import_execute"]
    assert params["payroll_net_payable_account_ref"]["jobs"] == ["import_execute"]
    # M1: gate values are per-dispatch inputs, never stored: always default "0".
    for gate in scoped.values():
        if gate["type"] == "enum":
            assert gate["default"] == "0", gate["material"]
    # The importer compares the two gates to the STRING "1": a bool would render
    # true/false and never approve, so they are enums of "0"/"1", default "0".
    cli = (ROOT / "src" / "erp_v2" / "importer" / "cli.py").read_text(encoding="utf-8")
    assert 'os.environ.get("ERP_IMPORT_APPROVED") != "1"' in cli
    assert 'os.environ.get("ERP_IMPORT_APPLY_OK") != "1"' in cli
    for name in ("import_approved", "import_apply_ok"):
        assert params[name]["type"] == "enum"
        assert params[name]["choices"] == ["0", "1"]
        assert params[name]["default"] == "0"
    # The actor id is parsed with uuid.UUID(...): a canonical dashed UUID.
    assert "_uuid.UUID(actor)" in cli
    actor = params["enrolment_actor_id"]
    assert actor["type"] == "string"
    pattern = re.compile(actor["pattern"])
    assert pattern.fullmatch("11111111-2222-3333-4444-555555555555")
    for bad in ("", "not-a-uuid", "11111111222233334444555555555555", "x" * 36):
        assert not pattern.fullmatch(bad), bad
    assert "default" not in actor  # optional: only void journals need it
    # The payable account ref is only stripped and required non-empty by the code
    # and stored in a String(240) column: bounded, no invented character set.
    payable = params["payroll_net_payable_account_ref"]
    assert (payable["type"], payable["min_length"], payable["max_length"]) == (
        "string",
        1,
        240,
    )
    assert "pattern" not in payable
    assert 'os.environ.get("ERP_PAYROLL_NET_PAYABLE_ACCOUNT_REF") or None' in cli
    # Scoped params never carry a secret-class material (the contract rule).
    for param in scoped.values():
        assert not SECRET_NAME_HINT.search(param["material"]), param["material"]
    broken = copy.deepcopy(unit)
    broken["params"]["import_approved"]["jobs"] = ["nope"]
    assert "param-job-unknown:import_approved" in _check(broken)
    unmaterial = copy.deepcopy(unit)
    del unmaterial["params"]["import_approved"]["material"]
    assert "param-job-material:import_approved" in _check(unmaterial)


_DSN = {"MIGRATION_DATABASE_URL": "MIGRATION", "LEGACY_DATABASE_URL": "LEGACY"}
#: Not operator commands: the schema upgrade (its own migrate credential, run by
#: Foundation), alembic's env, the serving runtime's refusal, and the shared
#: helper module (its readers are the modules that CALL it).
_DSN_EXEMPT = frozenset(
    {
        "erp_v2.migrate",
        "erp_v2.migrations.env",
        "erp_v2.database",
        "erp_v2.migration_ledger.cli_common",
    }
)
#: Packages whose `__main__` is their CLI are run as the package.
_PACKAGE_MAIN = {
    "erp_v2.importer.cli": "erp_v2.importer",
    "erp_v2.claims_opening.cli": "erp_v2.claims_opening",
}


def _is_docstring(node: ast.AST, parent: ast.AST | None) -> bool:
    return isinstance(parent, ast.Expr) and isinstance(node, ast.Constant) and isinstance(node.value, str)


def _dsn_reads(tree: ast.Module) -> set[str]:
    """Operator DSNs a module READS (not merely refuses): MIGRATION / LEGACY.

    A read is a `pop`/`get`/`getenv` of the name, a `default=` of an argparse
    option naming it, a loop that pops each name of a tuple, or a call to the
    shared helpers `cli_common.engine()` (MIGRATION) / `legacy_engine()` (LEGACY),
    resolved through the module's own import aliases. A bare `"X" in os.environ`
    is a refusal and is not a read.
    """
    helpers: set[str] = set()  # names bound to the cli_common module
    direct: dict[str, str] = {}  # engine/legacy_engine imported by name
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if node.module == "erp_v2.migration_ledger" and alias.name == "cli_common":
                    helpers.add(alias.asname or alias.name)
                if node.module == "erp_v2.migration_ledger.cli_common" and alias.name in {
                    "engine",
                    "legacy_engine",
                }:
                    direct[alias.asname or alias.name] = alias.name
    reads: set[str] = set()

    def names_in(node: ast.AST) -> set[str]:
        return {_DSN[n.value] for n in ast.walk(node) if isinstance(n, ast.Constant) and n.value in _DSN}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {"pop", "get", "getenv"}:
                if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in _DSN:
                    reads.add(_DSN[str(node.args[0].value)])
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in helpers:
                if func.attr == "engine":
                    reads.add("MIGRATION")
                if func.attr == "legacy_engine":
                    reads.add("LEGACY")
            if isinstance(func, ast.Name) and direct.get(func.id) == "engine":
                reads.add("MIGRATION")
            if isinstance(func, ast.Name) and direct.get(func.id) == "legacy_engine":
                reads.add("LEGACY")
            for kw in node.keywords:
                if kw.arg == "default" and isinstance(kw.value, ast.Constant) and kw.value.value in _DSN:
                    reads.add(_DSN[str(kw.value.value)])
        if isinstance(node, ast.For) and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "pop"
            for n in ast.walk(node)
        ):
            reads |= names_in(node.iter)
    return reads


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)


def _dsn_readers() -> dict[str, set[str]]:
    readers: dict[str, set[str]] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path.name == "__main__.py":
            continue
        module = _module_name(path)
        if module in _DSN_EXEMPT:
            continue
        found = _dsn_reads(ast.parse(path.read_text(encoding="utf-8")))
        if found:
            readers[_PACKAGE_MAIN.get(module, module)] = found
    return readers


def test_the_dsn_scan_itself_sees_each_way_of_reading_a_dsn() -> None:
    """The scan must not be vacuous: prove it on synthetic modules, both ways."""
    cases = {
        'import os\nx = os.environ.pop("MIGRATION_DATABASE_URL", "")': {"MIGRATION"},
        'import os\nx = os.getenv("LEGACY_DATABASE_URL")': {"LEGACY"},
        'def f(p):\n    p.add_argument("--s", default="LEGACY_DATABASE_URL")': {"LEGACY"},
        ("import os\nfor n in ('MIGRATION_DATABASE_URL', 'LEGACY_DATABASE_URL'):\n    os.environ.pop(n, None)"): {
            "MIGRATION",
            "LEGACY",
        },
        "from erp_v2.migration_ledger import cli_common as cli\ncli.engine()": {"MIGRATION"},
        "from erp_v2.migration_ledger import cli_common\ncli_common.legacy_engine()": {"LEGACY"},
        "from erp_v2.migration_ledger.cli_common import engine as e\ne()": {"MIGRATION"},
        # Refusals and prose are not reads.
        'import os\nif "LEGACY_DATABASE_URL" in os.environ:\n    raise SystemExit': set(),
        '"""Reads MIGRATION_DATABASE_URL."""': set(),
    }
    for source, expected in cases.items():
        assert _dsn_reads(ast.parse(source)) == expected, source


def test_every_command_that_reads_an_operator_dsn_is_an_import_role_job() -> None:
    """Q-F2 / E1, both directions, by AST (not grep).

    * Every module that reads an operator DSN is the command of at least one job
      (never left outside the import role).
    * What a module's jobs declare is exactly what the module reads: nothing
      missing (under-declared) and nothing extra (over-declared). A job with no
      role names no DSN at all.
    * No job runs a module that reads neither (the serving runner included)."""
    unit = _unit()
    by_module: dict[str, list[dict]] = {}
    for job in unit["jobs"]:
        assert job["trigger"] == "operator"
        assert job["command"][:2] == ["python", "-m"], job["name"]
        by_module.setdefault(job["command"][2], []).append(job)
    readers = _dsn_readers()
    for module in readers:
        assert module in by_module, f"{module} reads an operator DSN but is not a job"
    for module, jobs in by_module.items():
        assert module in readers, f"{module} is a job but reads no operator DSN"
        declared: set[str] = set()
        for job in jobs:
            dsns = {_DSN[m] for m in job["materials"] if m in _DSN}
            if job.get("role") is None:
                assert not dsns, f"role-less job {job['name']} names a DSN"
            else:
                assert job["role"] == "import", job["name"]
            declared |= dsns
        assert declared == readers[module], (module, declared, readers[module])
    assert "erp_v2.billing_gl" not in by_module  # the serving runner refuses operator DSNs
    assert len(by_module) >= 14


def _argparse_flags(tree: ast.Module) -> set[str]:
    """Every `--flag` a module's argparse accepts, loop-generated ones included."""
    flags: set[str] = set()

    def visit(node: ast.AST, env: dict[str, str]) -> None:
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, ast.Tuple | ast.List)
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.iter.elts)
        ):
            for element in node.iter.elts:
                for child in node.body:
                    visit(child, {**env, node.target.id: str(element.value)})  # type: ignore[attr-defined]
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
        ):
            first = node.args[0]
            flag = None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                flag = first.value
            elif (
                isinstance(first, ast.BinOp)
                and isinstance(first.left, ast.Constant)
                and isinstance(first.right, ast.Name)
            ):
                if first.right.id in env:
                    flag = str(first.left.value) + env[first.right.id]
            elif isinstance(first, ast.JoinedStr):
                parts = []
                for piece in first.values:
                    if isinstance(piece, ast.Constant):
                        parts.append(str(piece.value))
                    elif (
                        isinstance(piece, ast.FormattedValue)
                        and isinstance(piece.value, ast.Name)
                        and piece.value.id in env
                    ):
                        parts.append(env[piece.value.id])
                    else:
                        parts = []
                        break
                flag = "".join(parts) or None
            if flag and flag.startswith("--"):
                flags.add(flag)
        for child in ast.iter_child_nodes(node):
            visit(child, env)

    visit(tree, {})
    return flags


def test_job_arguments_are_declared_flags_of_the_job_module_and_closed() -> None:
    """E2/H2: every declared argument is a real flag of that module; the env-name
    overrides are never declared; files are confined to the job's work dir."""
    unit = _unit()
    for job in unit["jobs"]:
        module = job["command"][2]
        path = ROOT / "src" / Path(*module.split("."))
        source = path.with_suffix(".py") if path.with_suffix(".py").exists() else path / "cli.py"
        known = _argparse_flags(ast.parse(source.read_text(encoding="utf-8")))
        assert known, module
        for arg in job.get("args", []):
            assert arg["flag"] in known, (job["name"], arg["flag"])
            assert arg["flag"] not in NEVER_FLAGS
            if arg["type"] == "path":
                root = {"input": JOB_IN, "output": JOB_OUT}[arg["io"]]
                assert arg["pattern"].startswith(f"^{root}/")
                assert ".." not in arg["pattern"]
        # A plan or dry-run job cannot carry the apply flag.
        if job["name"].endswith(("_plan", "_census", "_verify", "_rounding")):
            assert "--apply" not in {a["flag"] for a in job.get("args", [])}, job["name"]
    # The NEVER flags exist in the importer (so the omission is a real choice).
    importer = _argparse_flags(ast.parse((ROOT / "src/erp_v2/importer/cli.py").read_text(encoding="utf-8")))
    assert NEVER_FLAGS <= importer
    broken = copy.deepcopy(unit)
    broken["jobs"][1]["args"] = [{"flag": "--source-url-env", "type": "string"}]
    assert any("job-arg-flag" in p for p in _check(broken))
    outside = copy.deepcopy(unit)
    first = next(j for j in outside["jobs"] if any(a["type"] == "path" for a in j.get("args", [])))
    next(a for a in first["args"] if a["type"] == "path")["pattern"] = "^/tmp/[a-z]+$"
    assert any("path-outside-workdir" in p for p in _check(outside))


def test_job_timeouts_are_declared_and_realistic() -> None:
    jobs = {j["name"]: j for j in _unit()["jobs"]}
    assert all(isinstance(j.get("timeout_seconds"), int) for j in jobs.values())
    assert jobs["import_execute"]["timeout_seconds"] >= 1800
    # A lease is timeout + 120: the long ones need an attended per-target ceiling
    # above the 900 s default; the short ones fit the default.
    short = [n for n, j in jobs.items() if j["timeout_seconds"] + 120 <= 900]
    assert len(short) > len(jobs) // 2
    assert max(j["timeout_seconds"] for j in jobs.values()) + 120 <= 3600


def test_job_scoped_gates_are_declared_only_for_jobs_that_read_them() -> None:
    unit = _unit()
    jobs = {j["name"] for j in unit["jobs"]}
    for name, param in unit["params"].items():
        for job in param.get("jobs", []):
            assert job in jobs, (name, job)
    # The apply gate of a job is enum 0/1 default 0: dry-run unless asked.
    for name, param in unit["params"].items():
        if param.get("jobs") and param["type"] == "enum":
            assert param["choices"] == ["0", "1"] and param["default"] == "0", name


def test_per_mode_materials_are_the_hand_reviewed_ones() -> None:
    """The both-way DSN scan is per module, so a MODE could still over-declare
    LEGACY. These were read mode by mode against the code: a mode names exactly
    the DSNs it reads, and one that reads none has no role at all."""
    jobs = {j["name"]: j for j in _unit()["jobs"]}
    mig, leg = "MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL"
    reviewed = {
        "import_census": [],  # writes the census receipt only; no database
        "import_billing_plan": [],  # reads JSON inputs only
        "import_billing_preflight": [],
        "import_plan": [leg],  # _take_env(source) only
        "import_execute": [mig, leg],
        "ar_opening_census": [leg],  # the destination DSN is optional there
        "ar_opening_mint": [mig, leg],
        "claims_opening_verify": [mig],  # `mode == "verify" or source`
        "claims_opening_plan": [mig, leg],
        "claims_opening_apply": [mig, leg],
        "ledger_bridge_build": [mig, leg],
        "ledger_bridge_sign": [mig],
        "ledger_bridge_rounding": [mig],
        "legacy_ap_review": [mig],
        "legacy_ap_cohort": [mig],
        "payees_bulk": [mig],
        "holdings_apply": [mig],
        "governance_apply": [mig],
        "access_apply": [mig],
        "cash_gl_setup": [mig],
        "payroll_accounts": [mig],
        "billing_gl_vat_opening_apply": [mig],
        "ledger_exceptions_gate": [mig],
        "held_drafts_plan": [mig, leg],
        "tax_seed": [mig, leg],
        "billing_gl_vat_extract": [mig, leg],
    }
    for name, expected in reviewed.items():
        assert sorted(jobs[name]["materials"]) == sorted(expected), name
        # No DSN, no role: nothing is minted for a command that reads no database.
        assert (jobs[name].get("role") == "import") is bool(expected), name


def test_the_import_uid_is_one_pinned_constant_used_everywhere() -> None:
    """R6-3: `dotmac-erp-import` has ONE fixed numeric uid (deploy/job-user.toml), not
    the serving uid, outside the block left to the serving app and companion; the job
    spec's container user and the image's /job/out ownership use exactly it."""
    pinned = tomllib.loads((ROOT / "deploy" / "job-user.toml").read_text())["import_job_user"]
    assert pinned["name"] == "dotmac-erp-import"
    uid, gid = pinned["uid"], pinned["gid"]
    assert isinstance(uid, int) and isinstance(gid, int) and uid == gid
    serving = _spec()["roles"][0]["security"]["user"]
    assert f"{uid}:{gid}" != serving
    assert 10040 <= uid <= 10049, "ERP operator-job users own 10040..10049"
    # Every uid the ERP product spec uses is outside that block.
    for role in _spec()["roles"]:
        used = int(role["security"]["user"].split(":")[0])
        assert not 10040 <= used <= 10049, role["code"]
    # The job's container user (provisional operator fixture) is exactly it.
    fixture = json.loads((ROOT / "tests" / "fixtures" / "operator_commands.provisional.json").read_text())
    assert fixture["security"]["user"] == f"{uid}:{gid}"
    assert fixture["user_name"] == pinned["name"]
    # The image creates it and gives /job/out to it at 0700.
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"groupadd --gid {gid} {pinned['name']}" in dockerfile
    assert f"useradd --uid {uid} --gid {gid}" in dockerfile
    assert "mkdir -p /job/in /job/out" in dockerfile
    assert f"chown {uid}:{gid} /job/out" in dockerfile
    assert f"chown {serving} /job/out" not in dockerfile
    assert "chmod 0700 /job/out" in dockerfile
    assert "chmod 0755 /job/in" in dockerfile
    # The descriptor's paths are exactly these two directories.
    for job in _unit()["jobs"]:
        for arg in job.get("args", []):
            if arg["type"] == "path":
                assert arg["pattern"].startswith("^/job/out/" if arg["io"] == "output" else "^/job/in/")


def test_each_job_command_equals_its_operator_command_both_ways() -> None:
    """PROVISIONAL: compared with a fixture copy until Foundation's product.toml
    carries the closed `operator.commands` map. The two-way equality is the check:
    no job without an operator command, no operator command without a job, and
    the argv prefix is identical."""
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "operator_commands.provisional.json").read_text(encoding="utf-8")
    )
    operator = fixture["commands"]
    jobs = {j["name"]: j["command"] for j in _unit()["jobs"]}
    assert set(jobs) == set(operator), (
        sorted(set(jobs) ^ set(operator)),
        "a job and an operator command must match one-to-one",
    )
    for name, command in jobs.items():
        assert operator[name] == command, name
    assert "provisional" in fixture


def test_job_user_constant_dockerfile_and_job_spec_user_are_the_same_value() -> None:
    """P-SEC rev7: deploy/job-user.toml, the Dockerfile's /job/out chown and the job's
    `security.user` (provisional fixture, until Foundation's operator table exists) are
    one value, 10040:10040, and none of them is the serving user."""
    pinned = tomllib.loads((ROOT / "deploy" / "job-user.toml").read_text())["import_job_user"]
    value = f"{pinned['uid']}:{pinned['gid']}"
    fixture = json.loads((ROOT / "tests" / "fixtures" / "operator_commands.provisional.json").read_text())
    chown = re.search(r"chown (\d+:\d+) /job/out", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert chown is not None
    assert {chown.group(1), fixture["security"]["user"], value} == {"10040:10040"}
    assert value != _spec()["roles"][0]["security"]["user"]
