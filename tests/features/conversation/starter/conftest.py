"""CHAT-US-01 테스트 공통 준비: 개발용 X-User-Id 헤더를 켠다(BASE-03)."""

import pytest


@pytest.fixture(autouse=True)
def dev_user_header(dev_user_header_enabled):
    """BASE-03: 이 폴더 테스트는 개발용 X-User-Id 헤더로 사용자를 정한다(tests/conftest.py)."""
