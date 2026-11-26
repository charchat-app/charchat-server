"""BASE-03 이메일·비밀번호 로그인(POST /auth/tokens grant_type=password).

카드 체크리스트: 로그인 성공, 틀린 비밀번호/없는 이메일 같은 401 응답
+ 없는 이메일도 bcrypt 검사 호출.
"""

import bcrypt
import pytest

from app.core.config import settings
from app.features.account.auth import passwords
from tests.features.account.account_helpers import (
    ME,
    STORIES,
    bearer,
    jwt_claims,
    onboard,
    password_login,
    signup,
)

EMAIL = "reader@example.com"
PASSWORD = "Moonlight42"


@pytest.fixture
def account(client):
    response = signup(client, EMAIL, PASSWORD)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def checkpw_calls(monkeypatch):
    """bcrypt.checkpw 호출을 기록하는 spy(실제 검사는 그대로 한다)."""
    calls = []
    real = bcrypt.checkpw

    def spy(password: bytes, hashed: bytes) -> bool:
        calls.append(hashed)
        return real(password, hashed)

    monkeypatch.setattr(bcrypt, "checkpw", spy)
    return calls


def test_password_login_succeeds_with_new_session_BASE_03(client, db, account):
    onboard(client, account["access_token"])
    response = password_login(client, "  READER@Example.com ", PASSWORD, "android")
    assert response.status_code == 201, response.text
    tokens = response.json()
    assert tokens["is_new_user"] is False
    assert tokens["user"]["user_id"] == account["user"]["user_id"]
    assert tokens["user"]["status"] == "active"
    assert tokens["user"]["providers"] == ["email"]
    assert tokens["refresh_token"] != account["refresh_token"]
    assert jwt_claims(tokens["access_token"])["sid"] != jwt_claims(account["access_token"])["sid"]
    assert client.get(STORIES, headers=bearer(tokens["access_token"])).status_code == 200
    platforms = db.execute("SELECT client_platform FROM auth_sessions ORDER BY id").fetchall()
    assert [row["client_platform"] for row in platforms] == ["web", "android"]


def test_pending_account_can_log_in_and_still_needs_onboarding_BASE_03(client, account):
    tokens = password_login(client, EMAIL, PASSWORD).json()
    assert tokens["user"]["status"] == "pending"
    me = client.get(ME, headers=bearer(tokens["access_token"]))
    assert me.json()["onboarding_required"] is True


def test_wrong_password_and_unknown_email_get_identical_401_BASE_03(client, db, account):
    sessions_before = db.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()["n"]
    wrong = password_login(client, EMAIL, "Moonlight43")
    unknown = password_login(client, "nobody@example.com", PASSWORD)
    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    assert wrong.json() == unknown.json()
    assert PASSWORD not in wrong.text and EMAIL not in wrong.text
    assert db.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()["n"] == sessions_before


def test_unknown_email_still_runs_bcrypt_check_BASE_03(client, account, checkpw_calls):
    response = password_login(client, "nobody@example.com", PASSWORD)
    assert response.status_code == 401
    assert len(checkpw_calls) == 1
    # 더미 해시도 같은 cost(12)라 검사 시간이 비슷하다.
    assert checkpw_calls[0].startswith(b"$2b$12$")

    checkpw_calls.clear()
    assert password_login(client, EMAIL, "Moonlight43").status_code == 401
    assert len(checkpw_calls) == 1


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("not-an-email", PASSWORD),  # 형식이 틀린 이메일도 같은 401(검사는 한다)
        (EMAIL, "가" * 24 + "a1"),  # 72바이트 초과(bcrypt에 넣지 않고 거절)
        (EMAIL, "Moon\x00light42"),
        (EMAIL, ""),
    ],
)
def test_unusable_credentials_are_same_401_and_still_checked_BASE_03(
    client, account, checkpw_calls, email, password
):
    response = password_login(client, email, password)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    assert len(checkpw_calls) == 1


def test_password_is_not_truncated_at_72_bytes_BASE_03(client):
    """앞 72바이트가 같아도 더 긴 비밀번호로는 로그인되지 않는다(bcrypt 잘림 방지)."""
    exact = "가" * 22 + "abcde1"  # 72바이트
    assert signup(client, "edge@example.com", exact).status_code == 201
    assert password_login(client, "edge@example.com", exact).status_code == 201
    assert password_login(client, "edge@example.com", exact + "x").status_code == 401


def test_deleted_account_cannot_log_in_with_password_BASE_03(client, db, account):
    db.execute(
        "UPDATE users SET status = 'deleted', deleted_at = clock_timestamp() WHERE id = %s",
        (account["user"]["user_id"],),
    )
    response = password_login(client, EMAIL, PASSWORD)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"


def test_password_login_is_404_when_disabled_BASE_03(client, account, monkeypatch):
    monkeypatch.setattr(settings, "auth_password_login_enabled", False)
    response = password_login(client, EMAIL, PASSWORD)
    assert response.status_code == 404
    assert response.json()["code"] == "AUTH_PASSWORD_LOGIN_DISABLED"


def test_check_password_helper_uses_dummy_hash_for_missing_account_BASE_03(checkpw_calls):
    assert passwords.check_password(PASSWORD, None) is False
    assert len(checkpw_calls) == 1
    stored = passwords.hash_password(PASSWORD)
    assert passwords.check_password(PASSWORD, stored) is True
    assert passwords.check_password(PASSWORD + "!", stored) is False
