"""BASE-03 로그인 토큰 발급·회전·재사용 탐지·로그아웃.

POST /auth/tokens, DELETE /auth/sessions/current.

카드 체크리스트: 로그아웃 후 401, 리프레시 회전, 재사용 시 세션 폐기.
"""

import hashlib
import threading
import time

import psycopg
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
    expired_access_token,
    jwt_claims,
    mint_access_token,
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


def test_providers_lists_email_and_dev_when_enabled_BASE_03(client, monkeypatch):
    email = {"provider": "email", "flows": ["password"]}
    dev = {"provider": "dev", "flows": ["direct"]}
    assert client.get(PROVIDERS).json() == {"providers": [email, dev]}
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    assert client.get(PROVIDERS).json() == {"providers": [email]}
    monkeypatch.setattr(settings, "auth_password_login_enabled", False)
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
    assert first["access_token"].startswith("eyJ")  # JWT
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
    assert jwt_claims(first["access_token"])["sid"] == str(rows[0]["id"])
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
    # 새 JWT는 같은 세션을 가리킨다. 이전 JWT도 세션이 살아 있으면 만료까지 쓰인다.
    assert jwt_claims(rotated["access_token"])["sid"] == jwt_claims(first["access_token"])["sid"]
    assert client.get(STORIES, headers=bearer(rotated["access_token"])).status_code == 200
    assert client.get(STORIES, headers=bearer(first["access_token"])).status_code == 200
    # 새 리프레시 토큰으로 다시 회전할 수 있다.
    assert refresh(client, rotated["refresh_token"]).status_code == 201


def test_refresh_after_access_expiry_restores_access_BASE_03(client, db):
    first = active_login(client, "alice")
    expired = expired_access_token(first["access_token"])
    assert client.get(STORIES, headers=bearer(expired)).json()["code"] == "AUTH_TOKEN_EXPIRED"
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


def test_logout_with_expired_access_token_revokes_session_BASE_03(client, db):
    """앱을 30분 넘게 켜 둔 뒤 로그아웃해도(액세스 토큰 만료) 서버 세션이 폐기된다."""
    first = active_login(client, "alice")
    expired = expired_access_token(first["access_token"])
    # 다른 경로에서는 그대로 EXPIRED다(로그아웃 경로만 만료를 허용).
    assert client.get(ME, headers=bearer(expired)).json()["code"] == "AUTH_TOKEN_EXPIRED"

    response = client.delete(CURRENT_SESSION, headers=bearer(expired))
    assert response.status_code == 204
    row = session_row(db, first["user"]["user_id"])
    assert row["revoked_at"] is not None
    assert row["revoke_reason"] == "logout"
    assert refresh(client, first["refresh_token"]).status_code == 401
    # 이미 폐기된 세션의 만료 토큰으로 다시 로그아웃하면 INVALID.
    again = client.delete(CURRENT_SESSION, headers=bearer(expired))
    assert again.status_code == 401
    assert again.json()["code"] == "AUTH_TOKEN_INVALID"


def test_logout_with_expired_but_forged_token_is_401_invalid_BASE_03(client, db):
    first = active_login(client, "alice")
    claims = jwt_claims(first["access_token"])
    old = int(time.time()) - 3600
    other_secret = mint_access_token(
        claims["sub"], claims["sid"], secret="other-" + "z" * 40, issued_at=old, ttl=1800
    )
    wrong_issuer = mint_access_token(
        claims["sub"], claims["sid"], issued_at=old, ttl=1800, iss="someone-else"
    )
    wrong_sub = mint_access_token(int(claims["sub"]) + 1, claims["sid"], issued_at=old, ttl=1800)
    expired = expired_access_token(first["access_token"])
    head, body, signature = expired.split(".")
    tampered = f"{head}.{body}.{signature[:-2]}{'AA' if signature[-2:] != 'AA' else 'BB'}"
    for token in (other_secret, wrong_issuer, wrong_sub, tampered):
        response = client.delete(CURRENT_SESSION, headers=bearer(token))
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "AUTH_TOKEN_INVALID"
    assert session_row(db, first["user"]["user_id"])["revoked_at"] is None


def test_concurrent_refresh_with_same_token_gives_one_201_and_one_409_BASE_03(client, db, db_url):
    """같은 리프레시 토큰으로 두 요청이 동시에 오면(행 잠금에서 함께 기다리게 만든 뒤 풀어 줌)
    하나만 회전에 성공(201)하고 다른 하나는 409다. 세션은 폐기되지 않는다."""
    first = active_login(client, "alice")
    user_id = first["user"]["user_id"]
    results: list[int] = []

    def send() -> None:
        results.append(refresh(client, first["refresh_token"]).status_code)

    with psycopg.connect(db_url) as holder:
        holder.execute("SELECT id FROM auth_sessions WHERE user_id = %s FOR UPDATE", (user_id,))
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = db.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND query LIKE '%%FOR UPDATE OF s%%'"
            ).fetchone()["n"]
            if waiting == 2:
                break
            time.sleep(0.05)
        assert waiting == 2, "두 요청이 같은 세션 행 잠금에서 함께 기다려야 한다"
        holder.rollback()  # 잠금을 풀어 두 요청을 동시에 진행시킨다
        for thread in threads:
            thread.join(timeout=10)

    assert sorted(results) == [201, 409]
    row = session_row(db, user_id)
    assert row["revoked_at"] is None
    assert row["previous_refresh_hash"] == sha256(first["refresh_token"])
