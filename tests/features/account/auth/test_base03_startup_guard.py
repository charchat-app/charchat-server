"""BASE-03 시작 가드: production에서 개발용 인증 설정이 켜져 있거나, staging·production에서
JWT 서명 키(AUTH_JWT_SECRET)가 없거나 32바이트 미만이면 앱을 만들지 않는다."""

import pytest
from pydantic import SecretStr

from app.core.config import Settings, settings
from app.main import create_app


@pytest.mark.parametrize(
    ("header", "login"),
    [(True, False), (False, True), (True, True)],
)
def test_production_refuses_dev_auth_flags_BASE_03(monkeypatch, header, login):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_dev_header_enabled", header)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", login)
    with pytest.raises(RuntimeError, match="AUTH_DEV_"):
        create_app()


def test_production_starts_without_dev_flags_BASE_03(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_dev_header_enabled", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    assert create_app() is not None


@pytest.mark.parametrize("env", ["local", "test", "staging"])
def test_non_production_allows_dev_flags_BASE_03(monkeypatch, env):
    monkeypatch.setattr(settings, "app_env", env)
    monkeypatch.setattr(settings, "auth_dev_header_enabled", True)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    assert create_app() is not None


def test_dev_auth_flags_default_off_BASE_03(monkeypatch):
    for name in ("APP_ENV", "AUTH_DEV_HEADER_ENABLED", "AUTH_DEV_LOGIN_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.app_env == "local"
    assert fresh.auth_dev_header_enabled is False
    assert fresh.auth_dev_login_enabled is False


def test_env_vars_reach_guard_BASE_03(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_DEV_HEADER_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "s" * 40)
    with pytest.raises(RuntimeError, match="AUTH_DEV_HEADER_ENABLED"):
        Settings(_env_file=None).assert_safe_for_startup()


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize("secret", ["", "short-secret", "x" * 31, "한" * 10])
def test_staging_and_production_refuse_missing_or_short_jwt_secret_BASE_03(
    monkeypatch, env, secret
):
    monkeypatch.setattr(settings, "app_env", env)
    monkeypatch.setattr(settings, "auth_dev_header_enabled", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_jwt_secret", SecretStr(secret))
    with pytest.raises(RuntimeError, match="AUTH_JWT_SECRET"):
        create_app()


@pytest.mark.parametrize("env", ["staging", "production"])
def test_staging_and_production_start_with_32_byte_jwt_secret_BASE_03(monkeypatch, env):
    monkeypatch.setattr(settings, "app_env", env)
    monkeypatch.setattr(settings, "auth_dev_header_enabled", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_jwt_secret", SecretStr("x" * 32))
    assert create_app() is not None


def test_production_env_without_jwt_secret_refuses_to_start_BASE_03(monkeypatch):
    """환경변수 경로: production은 임시 키를 만들지 않으므로 비어 있으면 거부한다."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.auth_jwt_secret.get_secret_value() == ""
    with pytest.raises(RuntimeError, match="AUTH_JWT_SECRET"):
        fresh.assert_safe_for_startup()
    monkeypatch.setenv("AUTH_JWT_SECRET", "s" * 40)
    Settings(_env_file=None).assert_safe_for_startup()


@pytest.mark.parametrize("env", ["local", "test"])
def test_local_and_test_generate_ephemeral_jwt_secret_when_empty_BASE_03(monkeypatch, env):
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    first = Settings(_env_file=None).auth_jwt_secret.get_secret_value()
    second = Settings(_env_file=None).auth_jwt_secret.get_secret_value()
    assert len(first.encode()) >= 48 and first != second  # 프로세스(설정)마다 새 임시 키
    Settings(_env_file=None).assert_safe_for_startup()
    monkeypatch.setenv("AUTH_JWT_SECRET", "configured-local-secret")
    assert Settings(_env_file=None).auth_jwt_secret.get_secret_value() == "configured-local-secret"


def test_jwt_secret_is_not_shown_in_settings_repr_BASE_03(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "do-not-print-this-secret-value-0123456789")
    fresh = Settings(_env_file=None)
    assert "do-not-print-this-secret" not in repr(fresh)
