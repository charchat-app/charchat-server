"""BASE-03 JWT 액세스 토큰: 발급 모양, 서명·알고리즘·클레임 검증, 세션 확인.

카드 체크리스트: 변조·alg:none·다른 secret·만료·sub/sid 불일치·폐기된 세션 각각 401,
로그아웃 즉시 401, 리프레시 회전이 새 JWT 발급.
"""

import base64
import json
import time

import jwt
import pytest
from pydantic import SecretStr

from app.core.config import settings
from tests.features.account.account_helpers import (
    CURRENT_SESSION,
    ME,
    STORIES,
    STORY_BODY,
    active_login,
    bearer,
    dev_login,
    expired_access_token,
    jwt_claims,
    mint_access_token,
    onboard,
    refresh,
)


def b64(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def assert_invalid(client, token: str, path: str = STORIES) -> None:
    response = client.get(path, headers=bearer(token))
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "AUTH_TOKEN_INVALID"


@pytest.fixture
def alice(client):
    tokens = active_login(client, "alice")
    claims = jwt_claims(tokens["access_token"])
    return {**tokens, "uid": int(claims["sub"]), "sid": int(claims["sid"])}


def test_access_token_is_hs256_jwt_with_contract_claims_BASE_03(client, alice):
    token = alice["access_token"]
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "HS256", "typ": "JWT"}
    claims = jwt.decode(
        token,
        settings.auth_jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer="charchat",
    )
    assert claims["iss"] == "charchat"
    assert claims["sub"] == str(alice["user"]["user_id"])
    assert claims["typ"] == "access"
    assert claims["exp"] - claims["iat"] == settings.auth_access_ttl_seconds == 1800
    assert abs(claims["iat"] - time.time()) < 60
    assert set(claims) >= {"iss", "sub", "sid", "iat", "exp", "typ"}


def test_self_minted_valid_token_is_accepted_BASE_03(client, alice):
    """테스트 도우미가 만든 토큰이 서버와 같은 모양인지(아래 거절 테스트의 기준선)."""
    token = mint_access_token(alice["uid"], alice["sid"])
    assert client.get(STORIES, headers=bearer(token)).status_code == 200


def test_tampered_payload_or_signature_is_401_BASE_03(client, alice):
    header, payload, signature = alice["access_token"].split(".")
    claims = jwt_claims(alice["access_token"])
    forged_payload = b64({**claims, "sub": "999"})
    assert_invalid(client, f"{header}.{forged_payload}.{signature}")
    middle = len(signature) // 2
    flipped = "A" if signature[middle] != "A" else "B"
    assert_invalid(
        client, f"{header}.{payload}.{signature[:middle]}{flipped}{signature[middle + 1 :]}"
    )
    assert_invalid(client, f"{header}.{payload}.")  # 서명 제거


@pytest.mark.parametrize("signature", ["", "c2lnbmF0dXJl"])
def test_alg_none_token_is_401_BASE_03(client, alice, signature):
    claims = jwt_claims(alice["access_token"])
    for header in ({"alg": "none", "typ": "JWT"}, {"alg": "None"}, {"alg": "NONE"}):
        assert_invalid(client, f"{b64(header)}.{b64(claims)}.{signature}")


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_other_algorithm_with_same_secret_is_401_BASE_03(client, alice, algorithm):
    assert_invalid(client, mint_access_token(alice["uid"], alice["sid"], algorithm=algorithm))


def test_token_signed_with_other_secret_is_401_BASE_03(client, alice):
    other = mint_access_token(alice["uid"], alice["sid"], secret="another-secret-" + "x" * 32)
    assert_invalid(client, other)
    # 만료된 토큰이라도 서명이 틀리면 EXPIRED가 아니라 INVALID다.
    expired_other = mint_access_token(
        alice["uid"],
        alice["sid"],
        secret="another-secret-" + "x" * 32,
        issued_at=int(time.time()) - 3600,
    )
    assert_invalid(client, expired_other)


def test_expired_token_is_401_auth_token_expired_BASE_03(client, alice):
    for path in (STORIES, ME):
        response = client.get(path, headers=bearer(expired_access_token(alice["access_token"])))
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_TOKEN_EXPIRED"
    # 앱은 리프레시로 회복한다.
    renewed = refresh(client, alice["refresh_token"]).json()
    assert client.get(STORIES, headers=bearer(renewed["access_token"])).status_code == 200


def test_sub_and_sid_mismatch_are_401_BASE_03(client, alice):
    bob = active_login(client, "bob")
    bob_sid = int(jwt_claims(bob["access_token"])["sid"])
    assert_invalid(client, mint_access_token(alice["uid"], bob_sid))  # 남의 세션
    assert_invalid(client, mint_access_token(bob["user"]["user_id"], alice["sid"]))  # sub 위조
    assert_invalid(client, mint_access_token(alice["uid"], 987654))  # 없는 세션


@pytest.mark.parametrize(
    ("drop", "overrides"),
    [
        (("exp",), {}),
        (("iat",), {}),
        (("sub",), {}),
        (("sid",), {}),
        (("iss",), {}),
        ((), {"iss": "someone-else"}),
        ((), {"typ": "refresh"}),
        (("typ",), {}),
        ((), {"sub": "abc"}),
        ((), {"sid": "0"}),
        ((), {"sid": 1}),
        ((), {"iat": int(time.time()) + 3600, "exp": int(time.time()) + 7200}),
    ],
)
def test_missing_or_wrong_claims_are_401_BASE_03(client, alice, drop, overrides):
    assert_invalid(client, mint_access_token(alice["uid"], alice["sid"], drop=drop, **overrides))


@pytest.mark.parametrize(
    "authorization",
    ["Bearer eyJ.abc", "Bearer a.b.c", "Bearer " + "eyJ" + "A" * 3000 + ".a.b", "Bearer x y z"],
)
def test_malformed_jwt_is_401_BASE_03(client, authorization):
    response = client.get(STORIES, headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_INVALID"


def test_revoked_session_and_deleted_account_jwt_are_401_BASE_03(client, db, alice):
    bob = active_login(client, "bob")
    db.execute(
        "UPDATE auth_sessions SET revoked_at = clock_timestamp(), revoke_reason = 'logout' "
        "WHERE id = %s",
        (alice["sid"],),
    )
    assert_invalid(client, alice["access_token"])
    assert_invalid(client, alice["access_token"], ME)
    db.execute(
        "UPDATE users SET status = 'deleted', deleted_at = clock_timestamp() WHERE id = %s",
        (bob["user"]["user_id"],),
    )
    assert_invalid(client, bob["access_token"])


def test_logout_makes_jwt_401_immediately_BASE_03(client, alice):
    assert client.get(STORIES, headers=bearer(alice["access_token"])).status_code == 200
    renewed = refresh(client, alice["refresh_token"]).json()
    assert client.delete(CURRENT_SESSION, headers=bearer(alice["access_token"])).status_code == 204
    # 만료 전이어도 세션이 폐기되면 바로 거절한다(회전 전·후 JWT 모두).
    assert_invalid(client, alice["access_token"])
    assert_invalid(client, renewed["access_token"])
    assert client.delete(CURRENT_SESSION, headers=bearer(alice["access_token"])).status_code == 401


def test_refresh_rotation_issues_new_jwt_for_same_session_BASE_03(client, alice):
    renewed = refresh(client, alice["refresh_token"])
    assert renewed.status_code == 201
    new_token = renewed.json()["access_token"]
    assert new_token != alice["access_token"]
    old_claims, new_claims = jwt_claims(alice["access_token"]), jwt_claims(new_token)
    assert new_claims["sid"] == old_claims["sid"]
    assert new_claims["sub"] == old_claims["sub"]
    assert new_claims["exp"] >= old_claims["exp"]
    assert client.get(STORIES, headers=bearer(new_token)).status_code == 200
    # 리뷰 포인트: 이전 JWT도 세션이 살아 있는 동안은 만료(30분)까지 쓰인다.
    assert client.get(STORIES, headers=bearer(alice["access_token"])).status_code == 200


@pytest.mark.parametrize("end", ["logout", "session_revoked"])
def test_requests_overlapping_rotation_use_old_jwt_until_session_ends_BASE_03(
    client, db, alice, end
):
    """회전과 겹친 요청: 회전 직후 이전 JWT로 보낸 목록 조회·저장이 성공한다.
    로그아웃·세션 폐기 뒤에는 이전·새 JWT 모두 401 AUTH_TOKEN_INVALID(재시도 없이 로그아웃 대상)."""
    old = bearer(alice["access_token"])
    renewed = refresh(client, alice["refresh_token"])
    assert renewed.status_code == 201
    new = bearer(renewed.json()["access_token"])
    assert client.get(STORIES, headers=old).status_code == 200
    saved = client.post(STORIES, json=STORY_BODY, headers=old)
    assert saved.status_code == 201, saved.text
    listed = client.get(STORIES, headers=new)
    assert [story["id"] for story in listed.json()] == [saved.json()["id"]]
    if end == "logout":
        assert client.delete(CURRENT_SESSION, headers=new).status_code == 204
    else:
        db.execute(
            "UPDATE auth_sessions SET revoked_at = clock_timestamp(), "
            "revoke_reason = 'refresh_reuse' WHERE id = %s",
            (alice["sid"],),
        )
    for headers in (old, new):
        for response in (
            client.get(STORIES, headers=headers),
            client.post(STORIES, json=STORY_BODY, headers=headers),
        ):
            assert response.status_code == 401, response.text
            assert response.json()["code"] == "AUTH_TOKEN_INVALID"
    assert db.execute("SELECT count(*) AS n FROM stories").fetchone()["n"] == 1


def test_jwt_secret_change_invalidates_existing_tokens_BASE_03(client, alice, monkeypatch):
    """서버 재시작으로 임시 키가 바뀌면 기존 JWT는 INVALID, 리프레시로 회복한다."""
    monkeypatch.setattr(settings, "auth_jwt_secret", SecretStr("rotated-secret-" + "y" * 32))
    assert_invalid(client, alice["access_token"])
    renewed = refresh(client, alice["refresh_token"]).json()
    assert client.get(STORIES, headers=bearer(renewed["access_token"])).status_code == 200


def test_pending_dev_account_jwt_reads_latest_status_BASE_03(client):
    """JWT에는 상태를 넣지 않고 매번 DB에서 읽는다: 온보딩 직후 같은 토큰으로 active 기능을 쓴다."""
    tokens = dev_login(client, "newbie")
    headers = bearer(tokens["access_token"])
    assert client.get(STORIES, headers=headers).json()["code"] == "ONBOARDING_REQUIRED"
    onboard(client, tokens["access_token"])
    assert client.get(STORIES, headers=headers).status_code == 200
