"""BASE-03 서버 계정 식별·권한 검사(공통 인증 의존성).

카드 체크리스트: 미인증 401, 만료 401, 변조·폐기 토큰 401, 개발용 헤더 위조 무시,
Bearer 우선, 다른 계정 자원 404, 온보딩 전 403.
"""

import pytest

from app.core.config import settings
from tests.features.account.account_helpers import (
    CONVERSATION_SESSIONS,
    ME,
    STORIES,
    active_login,
    bearer,
    create_story,
    dev_login,
    expired_access_token,
    seed_conversation_session,
)


def assert_problem(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == code


def test_request_without_credentials_is_401_BASE_03_unauthenticated(client):
    assert_problem(client.get(STORIES), 401, "UNAUTHENTICATED")
    assert_problem(client.get(ME), 401, "UNAUTHENTICATED")


def test_valid_access_token_identifies_account_BASE_03(client):
    tokens = active_login(client, "alice")
    response = client.get(STORIES, headers=bearer(tokens["access_token"]))
    assert response.status_code == 200
    me = client.get(ME, headers=bearer(tokens["access_token"])).json()
    assert me["user_id"] == tokens["user"]["user_id"]


def test_expired_access_token_is_401_auth_token_expired_BASE_03(client):
    tokens = active_login(client, "alice")
    expired = expired_access_token(tokens["access_token"])
    assert_problem(client.get(STORIES, headers=bearer(expired)), 401, "AUTH_TOKEN_EXPIRED")
    headers = bearer(expired)
    assert_problem(client.get(ME, headers=headers), 401, "AUTH_TOKEN_EXPIRED")


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer cca_" + "A" * 43,  # 형식은 맞지만 발급하지 않은 토큰
        "Bearer not-a-token",
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "cca_" + "A" * 43,  # Bearer 없이 토큰만
    ],
)
def test_unknown_or_malformed_token_is_401_auth_token_invalid_BASE_03(client, authorization):
    response = client.get(STORIES, headers={"Authorization": authorization})
    assert_problem(response, 401, "AUTH_TOKEN_INVALID")


def test_tampered_access_token_is_401_auth_token_invalid_BASE_03(client):
    token = active_login(client, "alice")["access_token"]
    header, payload, signature = token.split(".")
    # 서명 가운데 글자를 바꾼다(마지막 글자는 base64 패딩 비트라 바꿔도 같은 바이트일 수 있다).
    middle = len(signature) // 2
    flipped = "A" if signature[middle] != "A" else "B"
    tampered = f"{header}.{payload}.{signature[:middle]}{flipped}{signature[middle + 1 :]}"
    assert_problem(client.get(STORIES, headers=bearer(tampered)), 401, "AUTH_TOKEN_INVALID")
    # 리프레시 토큰을 액세스 토큰 자리에 넣어도 거절한다.
    refresh_token = dev_login(client, "alice")["refresh_token"]
    assert_problem(client.get(STORIES, headers=bearer(refresh_token)), 401, "AUTH_TOKEN_INVALID")


def test_revoked_session_token_is_401_auth_token_invalid_BASE_03(client, db):
    tokens = active_login(client, "alice")
    db.execute(
        "UPDATE auth_sessions SET revoked_at = clock_timestamp(), revoke_reason = 'logout' "
        "WHERE user_id = %s",
        (tokens["user"]["user_id"],),
    )
    assert_problem(
        client.get(STORIES, headers=bearer(tokens["access_token"])), 401, "AUTH_TOKEN_INVALID"
    )


def test_revoked_check_wins_over_expiry_BASE_03(client, db):
    """폐기된 세션은 만료됐어도 INVALID(앱이 리프레시하지 않고 로그아웃)."""
    tokens = active_login(client, "alice")
    db.execute(
        "UPDATE auth_sessions SET revoked_at = clock_timestamp(), revoke_reason = 'logout' "
        "WHERE user_id = %s",
        (tokens["user"]["user_id"],),
    )
    expired = expired_access_token(tokens["access_token"])
    assert_problem(client.get(STORIES, headers=bearer(expired)), 401, "AUTH_TOKEN_INVALID")


def test_deleted_account_token_is_401_auth_token_invalid_BASE_03(client, db):
    tokens = active_login(client, "alice")
    db.execute(
        "UPDATE users SET status = 'deleted', deleted_at = clock_timestamp() WHERE id = %s",
        (tokens["user"]["user_id"],),
    )
    assert_problem(
        client.get(STORIES, headers=bearer(tokens["access_token"])), 401, "AUTH_TOKEN_INVALID"
    )


def test_forged_dev_header_is_ignored_when_flag_off_BASE_03(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_dev_header_enabled", False)
    for path in (STORIES, ME, f"{CONVERSATION_SESSIONS}/1"):
        assert_problem(client.get(path, headers={"X-User-Id": "1"}), 401, "UNAUTHENTICATED")


def test_dev_header_is_off_by_default_in_tests_BASE_03(client):
    """개발용 헤더 폴더(conftest가 켬) 밖에서는 코드 기본값(꺼짐) 그대로: 헤더만 보내면 401."""
    assert settings.auth_dev_header_enabled is False
    for path in (STORIES, ME, f"{CONVERSATION_SESSIONS}/1"):
        assert_problem(client.get(path, headers={"X-User-Id": "1"}), 401, "UNAUTHENTICATED")
    assert_problem(
        client.patch(ME, headers={"X-User-Id": "1"}, json={"default_play_name": "민서"}),
        401,
        "UNAUTHENTICATED",
    )


def test_dev_header_still_works_when_flag_on_BASE_03(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_dev_header_enabled", True)
    assert client.get(STORIES, headers={"X-User-Id": "1"}).status_code == 200


@pytest.mark.parametrize("flag", [True, False])
def test_bearer_wins_over_dev_header_BASE_03(client, monkeypatch, flag):
    """Authorization과 X-User-Id를 함께 보내면 Authorization만 본다."""
    monkeypatch.setattr(settings, "auth_dev_header_enabled", flag)
    alice = active_login(client, "alice")
    bob = active_login(client, "bob")
    bob_story = create_story(client, bob["access_token"])
    headers = {**bearer(alice["access_token"]), "X-User-Id": str(bob["user"]["user_id"])}
    assert client.get(f"{STORIES}/{bob_story}", headers=headers).status_code == 404
    listed = client.get(STORIES, headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []
    me = client.get(ME, headers=headers).json()
    assert me["user_id"] == alice["user"]["user_id"]


def test_invalid_bearer_with_valid_dev_header_is_still_401_BASE_03(client, dev_user_header_enabled):
    headers = {"Authorization": "Bearer cca_" + "Z" * 43, "X-User-Id": "1"}
    assert_problem(client.get(STORIES, headers=headers), 401, "AUTH_TOKEN_INVALID")


def test_other_accounts_story_and_session_are_404_BASE_03(client, db):
    alice = active_login(client, "alice")
    bob = active_login(client, "bob")
    story = create_story(client, alice["access_token"])
    own_story = client.get(f"{STORIES}/{story}", headers=bearer(alice["access_token"]))
    assert own_story.status_code == 200
    assert_problem(
        client.get(f"{STORIES}/{story}", headers=bearer(bob["access_token"])),
        404,
        "STORY_NOT_FOUND",
    )

    session = seed_conversation_session(db, alice["user"]["user_id"])
    own = client.get(f"{CONVERSATION_SESSIONS}/{session}", headers=bearer(alice["access_token"]))
    assert own.status_code == 200, own.text
    other = client.get(f"{CONVERSATION_SESSIONS}/{session}", headers=bearer(bob["access_token"]))
    assert other.status_code == 404
    assert other.json()["code"].endswith("NOT_FOUND")
    listed = client.get(CONVERSATION_SESSIONS, headers=bearer(bob["access_token"]))
    assert listed.status_code == 200
    assert listed.json()["sessions"] == []


def test_pending_account_is_403_onboarding_required_but_can_use_me_BASE_03(client):
    tokens = dev_login(client, "newbie")
    assert tokens["user"]["status"] == "pending"
    headers = bearer(tokens["access_token"])
    assert_problem(client.get(STORIES, headers=headers), 403, "ONBOARDING_REQUIRED")
    assert_problem(client.post(STORIES, json={}, headers=headers), 403, "ONBOARDING_REQUIRED")
    assert_problem(
        client.get(f"{CONVERSATION_SESSIONS}/1", headers=headers), 403, "ONBOARDING_REQUIRED"
    )
    me = client.get(ME, headers=headers)
    assert me.status_code == 200
    assert me.json()["onboarding_required"] is True


def test_last_used_at_is_refreshed_only_when_stale_BASE_03(client, db):
    tokens = active_login(client, "alice")
    user_id = tokens["user"]["user_id"]
    db.execute(
        "UPDATE auth_sessions SET last_used_at = '2026-01-01T00:00:00Z' WHERE user_id = %s",
        (user_id,),
    )
    client.get(STORIES, headers=bearer(tokens["access_token"]))
    first = db.execute(
        "SELECT last_used_at FROM auth_sessions WHERE user_id = %s", (user_id,)
    ).fetchone()["last_used_at"]
    assert first.year > 2026 or first.month > 1
    client.get(STORIES, headers=bearer(tokens["access_token"]))
    second = db.execute(
        "SELECT last_used_at FROM auth_sessions WHERE user_id = %s", (user_id,)
    ).fetchone()["last_used_at"]
    assert second == first


def test_auth_resolves_before_request_connection_on_every_route_BASE_03():
    """토큰 확인은 풀에서 잠깐 빌린 커넥션을 쓴다. 요청 커넥션보다 먼저 풀려야 한 요청이
    커넥션 두 개를 동시에 잡지 않는다(풀 고갈 방지). 롱 폴링은 요청 커넥션을 받지 않는다."""
    from fastapi.routing import APIRoute

    from app.api import deps
    from app.db.session import get_conn
    from app.main import create_app

    auth_calls = {deps.get_current_user_id, deps.get_current_account, deps.get_optional_user_id}

    def order(dependant, out):
        for dep in dependant.dependencies:
            if dep.call in auth_calls:
                out.append("auth")
            elif dep.call is get_conn:
                out.append("conn")
            else:
                order(dep, out)
        return out

    def api_routes(routes):
        # 이 FastAPI 버전은 include_router 결과를 감싼 객체(original_router)로 둔다.
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                yield from api_routes(route.original_router.routes)

    checked = 0
    for route in api_routes(create_app().routes):
        steps = order(route.dependant, [])
        if "auth" in steps and "conn" in steps:
            checked += 1
            assert steps.index("auth") < steps.index("conn"), route.path
    assert checked > 20
