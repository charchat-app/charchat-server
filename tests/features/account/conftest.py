"""BASE-03 계정 테스트 공통 준비: dev 로그인을 켜고 토큰 수명·동의 버전을 기본값으로 고정한다."""

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def dev_login_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "auth_access_ttl_seconds", 1800)
    monkeypatch.setattr(settings, "auth_refresh_ttl_days", 60)
    monkeypatch.setattr(settings, "auth_terms_version", "terms-2026-11")
    monkeypatch.setattr(settings, "auth_privacy_version", "privacy-2026-11")
