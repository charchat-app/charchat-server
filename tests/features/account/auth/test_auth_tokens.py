"""BASE-03 로그인 토큰 발급·회전·재사용 탐지·로그아웃.

POST /auth/tokens, DELETE /auth/sessions/current.

카드 체크리스트: 로그아웃 후 401, 리프레시 회전, 재사용 시 세션 폐기.
"""

import hashlib

import pytest

from app.core.config import settings
from tests.features.account.account_helpers import (
    CURRENT_SESSION,
    ME,
    PROVIDERS,
    STORIES,
    TOKENS,
    active_login,
    bearer,
    dev_login,
    refresh,
)


def sha256(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def session_row(db, user_id: int):
    return db.execute("SELECT * FROM auth_sessions WHERE user_id = %s", (user_id,)).fetchone()


def age_rotation(db, user_id: int, seconds: int) -> None:
    db.execute(
        "UPDATE auth_sessions SET rotated_at = clock_timestamp() - make_interval(secs => %s) "
        "WHERE user_id = %s",
        (seconds, user_id),
    )


def test_providers_lists_dev_only_when_enabled_BASE_03(client, monkeypatch):
    assert client.get(PROVIDERS).json() == {"providers": [{"provider": "dev", "flows": ["direct"]}]}
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    assert client.get(PROVIDERS).json() == {"providers": []}


def test_dev_login_disabled_is_404_BASE_03(client, db, monkeypatch):
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    response = client.post(
        TOKENS, json={"grant_type": "dev", "dev_user_key": "1", "client_platform": "web"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "AUTH_DEV_LOGIN_DISABLED"
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0


def test_dev_login_creates_pending_account_once_and_stores_only_hashes_BASE_03(client, db):
    first = dev_login(client, "alice", "ios")
    assert first["token_type"] == "Bearer"
    assert first["access_token"].startswith("cca_")
    assert first["refresh_token"].startswith("ccr_")
    assert first["expires_in"] == 1800
    assert first["refresh_expires_in"] == 60 * 86400
    assert first["is_new_user"] is True
    assert first["user"]["status"] == "pending"
    assert first["user"]["providers"] == ["dev"]
    assert first["user"]["onboarding_required"] is True

    second = dev_login(client, "alice")
    assert second["is_new_user"] is False
    assert second["user"]["user_id"] == first["user"]["user_id"]
    assert second["access_token"] != first["access_token"]

    rows = db.execute("SELECT * FROM auth_sessions ORDER BY id").fetchall()
    assert [r["client_platform"] for r in rows] == ["ios", "web"]
    assert rows[0]["access_token_hash"] == sha256(first["access_token"])
    assert rows[0]["refresh_token_hash"] == sha256(first["refresh_token"])
    dump = repr(db.execute("SELECT * FROM auth_sessions").fetchall())
    assert first["access_token"] not in dump and first["refresh_token"] not in dump
    identity = db.execute("SELECT provider, provider_user_id FROM user_identities").fetchall()
    assert identity == [{"provider": "dev", "provider_user_id": "alice"}]


@pytest.mark.parametrize(
    "body",
    [
        {"grant_type": "dev", "dev_user_key": "", "client_platform": "web"},
        {"grant_type": "dev", "dev_user_key": "a b", "client_platform": "web"},
        {"grant_type": "dev", "dev_user_key": "x", "client_platform": "desktop"},
        {"grant_type": "password", "username": "x"},
        {"grant_type": "refresh_token"},
    ],
)
def test_token_request_validation_is_422_BASE_03(client, body):
    response = client.post(TOKENS, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_refresh_rotates_both_tokens_BASE_03(client, db):
    first = active_login(client, "alice")
    user_id = first["user"]["user_id"]
    response = refresh(client, first["refresh_token"])
    assert response.status_code == 201, response.text
    rotated = response.json()
    assert rotated["is_new_user"] is False
    assert rotated["user"]["status"] == "active"
    assert rotated["access_token"] != first["access_token"]
    assert rotated["refresh_token"] != first["refresh_token"]

    row = session_row(db, user_id)
    assert row["refresh_token_hash"] == sha256(rotated["refresh_token"])
    assert row["previous_refresh_hash"] == sha256(first["refresh_token"])
    assert row["rotated_at"] is not None
    # 새 액세스 토큰은 쓰이고 이전 액세스 토큰은 더 이상 쓰이지 않는다.
    assert client.get(STORIES, headers=bearer(rotated["access_token"])).status_code == 200
    old = client.get(STORIES, headers=bearer(first["access_token"]))
    assert old.status_code == 401
    assert old.json()["code"] == "AUTH_TOKEN_INVALID"
    # 새 리프레시 토큰으로 다시 회전할 수 있다.
    assert refresh(client, rotated["refresh_token"]).status_code == 201


def test_refresh_after_access_expiry_restores_access_BASE_03(client, db):
    first = active_login(client, "alice")
    db.execute("UPDATE auth_sessions SET access_expires_at = clock_timestamp()")
    assert client.get(STORIES, headers=bearer(first["access_token"])).json()["code"] == (
        "AUTH_TOKEN_EXPIRED"
    )
    rotated = refresh(client, first["refresh_token"]).json()
    assert client.get(STORIES, headers=bearer(rotated["access_token"])).status_code == 200


def test_old_refresh_reuse_within_grace_is_409_without_revoke_BASE_03(client, db):
    first = active_login(client, "alice")
    rotated = refresh(client, first["refresh_token"]).json()

    again = refresh(client, first["refresh_token"])
    assert again.status_code == 409
    assert again.json()["code"] == "AUTH_REFRESH_CONFLICT"
    row = session_row(db, first["user"]["user_id"])
    assert row["revoked_at"] is None
    assert client.get(STORIES, headers=bearer(rotated["access_token"])).status_code == 200
    assert refresh(client, rotated["refresh_token"]).status_code == 201


def test_old_refresh_reuse_after_grace_revokes_session_BASE_03(client, db):
    first = active_login(client, "alice")
    user_id = first["user"]["user_id"]
    rotated = refresh(client, first["refresh_token"]).json()
    age_rotation(db, user_id, 11)

    reuse = refresh(client, first["refresh_token"])
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "AUTH_REFRESH_INVALID"
    row = session_row(db, user_id)
    assert row["revoke_reason"] == "refresh_reuse"
    # 정상 사용자가 가진 최신 토큰도 함께 끊긴다(탈취 대응).
    current = client.get(STORIES, headers=bearer(rotated["access_token"]))
    assert current.status_code == 401
    assert current.json()["code"] == "AUTH_TOKEN_INVALID"
    assert refresh(client, rotated["refresh_token"]).status_code == 401


def test_unknown_malformed_or_revoked_refresh_is_401_BASE_03(client, db):
    for token in ("ccr_" + "A" * 43, "garbage", "cca_" + "A" * 43):
        response = refresh(client, token)
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_REFRESH_INVALID"
    first = active_login(client, "alice")
    db.execute("UPDATE auth_sessions SET revoked_at = clock_timestamp(), revoke_reason = 'logout'")
    assert refresh(client, first["refresh_token"]).status_code == 401


def test_expired_refresh_is_401_and_revokes_BASE_03(client, db):
    first = active_login(client, "alice")
    db.execute("UPDATE auth_sessions SET refresh_expires_at = clock_timestamp()")
    response = refresh(client, first["refresh_token"])
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REFRESH_INVALID"
    assert session_row(db, first["user"]["user_id"])["revoke_reason"] == "expired"


def test_logout_revokes_current_session_only_BASE_03(client, db):
    phone = active_login(client, "alice")
    laptop = dev_login(client, "alice")
    response = client.delete(CURRENT_SESSION, headers=bearer(phone["access_token"]))
    assert response.status_code == 204
    after = client.get(STORIES, headers=bearer(phone["access_token"]))
    assert after.status_code == 401
    assert after.json()["code"] == "AUTH_TOKEN_INVALID"
    assert client.get(ME, headers=bearer(phone["access_token"])).status_code == 401
    assert refresh(client, phone["refresh_token"]).status_code == 401
    # 같은 계정의 다른 기기 세션은 그대로다.
    assert client.get(ME, headers=bearer(laptop["access_token"])).status_code == 200
    reasons = db.execute("SELECT revoke_reason FROM auth_sessions ORDER BY id").fetchall()
    assert [r["revoke_reason"] for r in reasons] == ["logout", None]


def test_pending_account_can_logout_BASE_03(client):
    pending = dev_login(client, "newbie")
    response = client.delete(CURRENT_SESSION, headers=bearer(pending["access_token"]))
    assert response.status_code == 204


def test_logout_without_bearer_is_401_BASE_03(client):
    assert client.delete(CURRENT_SESSION).status_code == 401
    response = client.delete(CURRENT_SESSION, headers={"X-User-Id": "1"})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
