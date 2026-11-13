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


def anon_env(**extra):
    env = valid_env()
    env.update(
        {
            "ANONYMOUS_RATE_LIMIT_ROUTES": "/login",
            "ANONYMOUS_RATE_LIMIT_STORE": "postgres",
            "ANONYMOUS_RATE_LIMIT_KEY_SECRET": "fixture-anon-" + "a" * 32,
        }
    )
    env.update(extra)
    return env


def test_anonymous_key_ignored_when_routes_undeclared():
    env = valid_env()
    env["ANONYMOUS_RATE_LIMIT_STORE"] = "postgres"
    env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"] = "dev-insecure-change-me"  # noqa: S105
    enforce_runtime_materials(env)


def test_anonymous_key_ignored_on_memory_store():
    env = anon_env(ANONYMOUS_RATE_LIMIT_STORE="memory")
    del env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"]
    enforce_runtime_materials(env)


def test_valid_anonymous_key_allows_startup():
    enforce_runtime_materials(anon_env())


@pytest.mark.parametrize("value", [None, "dev-insecure-change-me", "short", "JWT", "SESSION", "CSRF", "MACHINE"])
def test_unsafe_anonymous_key_refuses_without_disclosure(value):
    env = anon_env(MACHINE_CREDENTIAL_HMAC_KEY="fixture-machine-" + "m" * 32)
    reuse = {
        "JWT": "JWT_SECRET",
        "SESSION": "SESSION_HASH_SECRET",
        "CSRF": "CSRF_SECRET",
        "MACHINE": "MACHINE_CREDENTIAL_HMAC_KEY",
    }
    if value is None:
        del env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"]
    elif value in reuse:
        env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"] = env[reuse[value]]
    else:
        env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"] = value
    with pytest.raises(RuntimeMaterialsError, match="ANONYMOUS_RATE_LIMIT_KEY_SECRET") as exc:
        enforce_runtime_materials(env)
    if value:
        assert env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"] not in str(exc.value)


def test_anonymous_key_rule_applies_when_store_unset():
    env = anon_env()
    del env["ANONYMOUS_RATE_LIMIT_STORE"]
    del env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"]
    with pytest.raises(RuntimeMaterialsError, match="ANONYMOUS_RATE_LIMIT_KEY_SECRET"):
        enforce_runtime_materials(env)


def test_anonymous_key_rule_applies_to_padded_uppercase_store():
    env = anon_env(ANONYMOUS_RATE_LIMIT_STORE=" POSTGRES ")
    del env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"]
    with pytest.raises(RuntimeMaterialsError, match="ANONYMOUS_RATE_LIMIT_KEY_SECRET"):
        enforce_runtime_materials(env)


def test_whitespace_only_routes_leave_behaviour_unchanged():
    env = anon_env(ANONYMOUS_RATE_LIMIT_ROUTES="   ")
    del env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"]
    enforce_runtime_materials(env)


def test_anonymous_key_rule_applies_without_database_url():
    env = {"ANONYMOUS_RATE_LIMIT_ROUTES": "/login", "ANONYMOUS_RATE_LIMIT_STORE": "postgres"}
    with pytest.raises(RuntimeMaterialsError, match="ANONYMOUS_RATE_LIMIT_KEY_SECRET"):
        enforce_runtime_materials(env)
    env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"] = "fixture-anon-" + "a" * 32
    enforce_runtime_materials(env)


def test_lowercase_anonymous_routes_with_missing_key_refuses():
    env = valid_env()
    env["anonymous_rate_limit_routes"] = "/login"
    with pytest.raises(RuntimeMaterialsError, match="ANONYMOUS_RATE_LIMIT_KEY_SECRET"):
        enforce_runtime_materials(env)


@pytest.mark.parametrize("value", ["short", "dev-insecure-change-me"])
def test_lowercase_signing_secret_is_refused_like_uppercase(value):
    env = valid_env()
    del env["JWT_SECRET"]
    env["jwt_secret"] = value
    with pytest.raises(RuntimeMaterialsError, match="JWT_SECRET") as exc:
        enforce_runtime_materials(env)
    assert value not in str(exc.value)


def test_case_collision_refuses_without_disclosure():
    env = valid_env()
    other = "fixture-other-" + "q" * 32
    env["jwt_secret"] = other
    with pytest.raises(RuntimeMaterialsError, match="JWT_SECRET") as exc:
        enforce_runtime_materials(env)
    assert other not in str(exc.value)
    assert env["JWT_SECRET"] not in str(exc.value)


def test_lowercase_weak_signing_secret_reports_exact_weak_message():
    env = valid_env()
    del env["JWT_SECRET"]
    weak = "short"
    env["jwt_secret"] = weak
    with pytest.raises(RuntimeMaterialsError) as exc:
        enforce_runtime_materials(env)
    assert str(exc.value) == "JWT_SECRET must contain at least 32 bytes"


def test_case_collision_message_is_exact():
    env = valid_env()
    env["jwt_secret"] = "fixture-other-" + "q" * 32
    with pytest.raises(RuntimeMaterialsError) as exc:
        enforce_runtime_materials(env)
    assert str(exc.value) == "JWT_SECRET is set under more than one letter case; supply exactly one spelling"


def test_only_lowercase_spellings_with_strong_distinct_values_pass():
    env = {key.lower(): value for key, value in valid_env().items()}
    env["anonymous_rate_limit_routes"] = "/login"
    env["anonymous_rate_limit_store"] = "postgres"
    env["anonymous_rate_limit_key_secret"] = "fixture-anon-" + "a" * 32
    env["machine_credential_hmac_key"] = "fixture-machine-" + "m" * 32
    enforce_runtime_materials(env)


def test_non_ascii_fold_lookalike_does_not_satisfy_gate_key():
    env = valid_env()
    del env["JWT_SECRET"]
    env["JWT_\u017fECRET"] = "fixture-jwt-" + "x" * 32
    with pytest.raises(RuntimeMaterialsError) as exc:
        enforce_runtime_materials(env)
    assert str(exc.value) == "JWT_SECRET must be supplied when a database is bound"


def test_kelvin_sign_name_is_treated_as_the_gate_key_like_the_kernel():
    env = anon_env()
    del env["ANONYMOUS_RATE_LIMIT_KEY_SECRET"]
    enforce_runtime_materials({**env, "ANONYMOUS_RATE_LIMIT_\u212aEY_SECRET": "fixture-anon-" + "a" * 32})
    with pytest.raises(RuntimeMaterialsError, match="ANONYMOUS_RATE_LIMIT_KEY_SECRET"):
        enforce_runtime_materials({**env, "ANONYMOUS_RATE_LIMIT_\u212aEY_SECRET": "short"})
