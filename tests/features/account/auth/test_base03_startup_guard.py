"""BASE-03 production 시작 가드: 개발용 인증 설정이 켜져 있으면 앱을 만들지 않는다."""

import pytest

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
    with pytest.raises(RuntimeError):
        Settings(_env_file=None).assert_safe_for_startup()
