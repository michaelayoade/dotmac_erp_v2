"""Serving processes refuse missing or unsafe session materials."""

import pytest

from erp_v2.materials import RuntimeMaterialsError, enforce_runtime_materials


def valid_env():
    return {
        "DATABASE_URL": "postgresql://runtime:fixture@localhost/fixture",
        "JWT_SECRET": "fixture-jwt-" + "x" * 32,
        "SESSION_HASH_SECRET": "fixture-session-" + "y" * 32,
        "CSRF_SECRET": "fixture-csrf-" + "z" * 32,
        "TRUSTED_HOSTS": "erp.example.test",
        "SEED_ON_STARTUP": "false",
    }


def test_valid_materials_allow_startup():
    enforce_runtime_materials(valid_env())


@pytest.mark.parametrize(
    "name", ["JWT_SECRET", "SESSION_HASH_SECRET", "CSRF_SECRET", "TRUSTED_HOSTS", "SEED_ON_STARTUP"]
)
def test_missing_material_refuses_without_disclosing_other_values(name):
    env = valid_env()
    del env[name]
    with pytest.raises(RuntimeMaterialsError, match=name) as exc:
        enforce_runtime_materials(env)
    for key in ("DATABASE_URL", "JWT_SECRET", "SESSION_HASH_SECRET", "CSRF_SECRET"):
        if key in env:
            assert env[key] not in str(exc.value)


@pytest.mark.parametrize("value", ["true", "yes", "invalid"])
def test_seeding_or_ambiguous_setting_refuses(value):
    env = valid_env()
    env["SEED_ON_STARTUP"] = value
    with pytest.raises(RuntimeMaterialsError, match="SEED_ON_STARTUP"):
        enforce_runtime_materials(env)


@pytest.mark.parametrize("value", ["short", "dev-insecure-change-me"])
def test_unsafe_signing_material_refuses(value):
    env = valid_env()
    env["JWT_SECRET"] = value
    with pytest.raises(RuntimeMaterialsError, match="JWT_SECRET"):
        enforce_runtime_materials(env)


def test_csrf_cannot_reuse_session_secret():
    env = valid_env()
    env["CSRF_SECRET"] = env["SESSION_HASH_SECRET"]
    with pytest.raises(RuntimeMaterialsError, match="distinct"):
        enforce_runtime_materials(env)
