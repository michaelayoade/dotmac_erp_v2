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
    (job,) = [j for j in unit["jobs"] if j["name"] == "import"]
    assert job["role"] == "import"
    assert job["materials"] == ["MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL"]
    by_name = {b["name"]: b for b in unit["bindings"]}
    assert by_name["MIGRATION_DATABASE_URL"]["roles"] == ["migrate", "import"]
    legacy = by_name["LEGACY_DATABASE_URL"]
    assert legacy["state"] == "pre_staged"
    assert legacy["kind"] == "pg_login_dsn"
    assert legacy["roles"] == ["import"]
    assert legacy["writer"] == "ceremony"
    assert legacy["rotation"] == "copy_only"
    # PROVISIONAL: P-SEC rev4 sets the final value. A role's secret_id is
    # single-use if ANY binding in it is, so `import` is single-use through
    # MIGRATION_DATABASE_URL whatever this says.
    assert legacy["single_use"] is False
    assert by_name["MIGRATION_DATABASE_URL"]["single_use"] is True
    # The serving app never gets either credential.
    for name in ("MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL"):
        assert "app" not in by_name[name]["roles"], name
    unknown = copy.deepcopy(unit)
    unknown["jobs"][0]["role"] = "ghost"
    assert "job-role-unknown:import" in _check(unknown)


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
        scoped["params"]["mine"] = {"type": "path", "material": name, "jobs": ["import"]}
        assert not any("param-job-only-material" in p for p in _check(scoped)), name
    # Nor is either a product (Foundation) runtime material of the serving app.
    (app_role,) = [r for r in _spec()["roles"] if r["code"] == "app"]
    assert "LEGACY_DATABASE_URL" not in app_role["materials"]
    assert "LEGACY_DATABASE_URL" not in _spec()["runtime_materials"]["names"]


def test_import_params_are_job_scoped_and_typed_as_the_importer_reads_them() -> None:
    unit = _unit()
    params = unit["params"]
    scoped = {n: p for n, p in params.items() if p.get("jobs")}
    assert {p["material"] for p in scoped.values()} == {
        "ERP_IMPORT_APPROVED",
        "ERP_IMPORT_APPLY_OK",
        "ERP_ENROLMENT_ACTOR_ID",
        "ERP_PAYROLL_NET_PAYABLE_ACCOUNT_REF",
    }
    assert all(p["jobs"] == ["import"] for p in scoped.values())
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
