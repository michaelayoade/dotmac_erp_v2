"""admin (and everyone) cannot write the treasury_policy setting through the ERP.

Bank file generation is OFF by default and only provisioning code (not a route, not a role) can turn it
on. This pins that: no mounted route writes settings, no permission names settings, admin/staff grants are
unchanged, and the only setting writer in the ERP source never touches the treasury_policy domain.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import replace

from dotmac_kernel import create_app

from erp_v2 import access
from erp_v2.assembly import assembly
from erp_v2.disbursement import policy

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "erp_v2"


def test_no_mounted_route_writes_settings():
    app = create_app(replace(assembly, web_enabled=True, database_runtime=None, require_database_runtime=False))
    writers = [
        (route.path, sorted(route.methods))
        for route in app.routes
        if "setting" in getattr(route, "path", "").lower()
        and set(getattr(route, "methods", None) or ()) - {"GET", "HEAD", "OPTIONS"}
    ]
    assert writers == []


def test_admin_and_staff_hold_no_settings_permission_and_nothing_names_one():
    (profile,) = assembly.role_grant_profiles
    for role in ("admin", "staff"):
        held = {g.permission_code for g in profile.grants if g.role_code == role}
        assert not any("setting" in code or "treasury" in code for code in held), (role, held)
    assert not any("setting" in spec.code for spec in access.PERMISSION_SPECS)
    for module in assembly.modules:
        for spec in getattr(module, "permissions", ()):
            if "setting" in spec.code and policy.TREASURY_POLICY_DOMAIN in spec.code:
                raise AssertionError(spec.code)


def test_only_provisioning_code_writes_settings_and_never_the_treasury_domain():
    writers = {}
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        text = path.read_text()
        if re.search(r"\b(upsert_by_key|ensure_by_key|clear_by_key)\b", text):
            writers[str(path.relative_to(SRC))] = text
    assert set(writers) == {"journals/policy.py"}
    assert "treasury_policy" not in writers["journals/policy.py"]
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if "BANK_FILE_KEY" in text or "TREASURY_POLICY_DOMAIN" in text:
            assert path.name in {"policy.py", "routes.py"}, path  # read-only users of the spec


def test_the_setting_defaults_off_and_is_a_boolean():
    assert policy.BANK_FILE_SPEC.default is False
    assert str(policy.BANK_FILE_SPEC.value_type) == "boolean" or policy.BANK_FILE_SPEC.value_type.value == "boolean"
