"""BASE-03 이메일·비밀번호 회원가입(POST /auth/signup).

카드 체크리스트: 가입 201·pending·온보딩 뒤 active, 이메일 대소문자·공백 정규화와 중복 409
(동시 가입 2건 → 하나만 성공), 비밀번호 규칙 각각 422(72바이트 초과 포함),
DB에 평문 없음·$2b$ 해시·cost 12.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import bcrypt
import pytest

from app.core.config import settings
from app.features.account.auth import repository
from tests.features.account.account_helpers import (
    ME,
    SIGNUP,
    STORIES,
    bearer,
    jwt_claims,
    onboard,
    signup,
)

PASSWORD = "Moonlight42"


def credentials(db):
    return db.execute(
        "SELECT user_id, email, password_hash FROM user_password_credentials ORDER BY user_id"
    ).fetchall()


def assert_problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == code
    return body


def test_signup_is_201_pending_and_onboarding_makes_active_BASE_03(client, db):
    response = signup(client, "reader@example.com", PASSWORD, "ios")
    assert response.status_code == 201, response.text
    tokens = response.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["is_new_user"] is True
    assert tokens["expires_in"] == 1800
    assert tokens["refresh_token"].startswith("ccr_")
    assert tokens["user"]["status"] == "pending"
    assert tokens["user"]["onboarding_required"] is True
    assert tokens["user"]["providers"] == ["email"]
    claims = jwt_claims(tokens["access_token"])
    assert claims["sub"] == str(tokens["user"]["user_id"])

    headers = bearer(tokens["access_token"])
    # 온보딩 전에는 /me만 쓸 수 있다.
    assert client.get(STORIES, headers=headers).json()["code"] == "ONBOARDING_REQUIRED"
    me = client.get(ME, headers=headers).json()
    assert me["status"] == "pending" and me["providers"] == ["email"]

    onboard(client, tokens["access_token"])
    assert client.get(ME, headers=headers).json()["status"] == "active"
    assert client.get(STORIES, headers=headers).status_code == 200
    # 이메일 계정은 user_identities에 행을 만들지 않는다.
    assert db.execute("SELECT count(*) AS n FROM user_identities").fetchone()["n"] == 0
    session = db.execute("SELECT id, client_platform FROM auth_sessions").fetchone()
    assert session == {"id": int(claims["sid"]), "client_platform": "ios"}


def test_signup_email_is_trimmed_and_lowercased_and_duplicate_is_409_BASE_03(client, db):
    first = signup(client, "  Reader.One@Example.COM ", PASSWORD)
    assert first.status_code == 201, first.text
    assert [row["email"] for row in credentials(db)] == ["reader.one@example.com"]

    for variant in ("reader.one@example.com", "READER.ONE@EXAMPLE.COM", "\treader.one@example.com"):
        body = assert_problem(signup(client, variant, "Another99"), 409, "EMAIL_ALREADY_REGISTERED")
        assert PASSWORD not in str(body) and "Another99" not in str(body)
    # 진 쪽 가입은 계정·세션을 남기지 않는다.
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1
    assert db.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()["n"] == 1


def test_concurrent_signups_with_same_email_only_one_succeeds_BASE_03(client, db, monkeypatch):
    """두 가입 요청이 같은 순간 이메일을 넣게 맞춘다. 유일 제약으로 하나만 201, 다른 하나는 409."""
    barrier = threading.Barrier(2, timeout=30)
    original = repository.create_password_credential

    def racing_insert(*args, **kwargs):
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "create_password_credential", racing_insert)
    emails = ["twin@example.com", "TWIN@example.com"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda email: signup(client, email, PASSWORD), emails))

    assert sorted(r.status_code for r in responses) == [201, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["code"] == "EMAIL_ALREADY_REGISTERED"
    assert [row["email"] for row in credentials(db)] == ["twin@example.com"]
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1
    assert db.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()["n"] == 1


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "plainaddress",
        "no-at.example.com",
        "two@@example.com",
        "user@nodot",
        "user name@example.com",
        "user@exa mple.com",
        "@example.com",
        "user@.",
        "a" * 245 + "@example.com",  # 257자
    ],
)
def test_signup_invalid_email_is_422_email_invalid_BASE_03(client, db, email):
    assert_problem(signup(client, email, PASSWORD), 422, "EMAIL_INVALID")
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0


def test_signup_accepts_254_character_email_BASE_03(client):
    email = "a" * 242 + "@example.com"
    assert len(email) == 254
    assert signup(client, email, PASSWORD).status_code == 201


@pytest.mark.parametrize(
    ("password", "hint"),
    [
        ("abc1234", "8~64자"),  # 7자
        ("a1" * 32 + "x", "8~64자"),  # 65자
        ("abcdefgh", "숫자"),
        ("12345678", "영문자"),
        ("비밀번호는한글만이야", "영문자"),
        ("abcd\x001234", "NUL"),
        ("가" * 24 + "a1", "72바이트"),  # 26자지만 UTF-8 72 + 2 = 74바이트
        ("", "8~64자"),
    ],
)
def test_signup_password_rules_are_422_password_invalid_BASE_03(client, db, password, hint):
    body = assert_problem(signup(client, "rules@example.com", password), 422, "PASSWORD_INVALID")
    assert hint in body["detail"]
    if password:
        assert password not in body["detail"]
    assert credentials(db) == []


def test_signup_password_at_limits_is_accepted_BASE_03(client, db):
    """8자, 64자, 정확히 72바이트, 앞뒤 공백(그대로 해시)은 받는다."""
    cases = {
        "min@example.com": "abcdefg1",
        "max@example.com": "a1" * 32,
        "bytes@example.com": "가" * 22 + "abcde1",  # 66 + 6 = 72바이트
        "space@example.com": "  abcd1234  ",
    }
    for email, password in cases.items():
        assert signup(client, email, password).status_code == 201, email
    stored = {row["email"]: row["password_hash"] for row in credentials(db)}
    assert bcrypt.checkpw(b"  abcd1234  ", stored["space@example.com"].encode())
    assert not bcrypt.checkpw(b"abcd1234", stored["space@example.com"].encode())


def test_signup_stores_only_bcrypt_cost_12_hash_BASE_03(client, db):
    response = signup(client, "hash@example.com", PASSWORD)
    assert response.status_code == 201
    [row] = credentials(db)
    assert row["password_hash"].startswith("$2b$12$")
    assert len(row["password_hash"]) == 60
    assert bcrypt.checkpw(PASSWORD.encode(), row["password_hash"].encode())
    for table in ("users", "user_password_credentials", "auth_sessions"):
        dump = repr(db.execute(f"SELECT * FROM {table}").fetchall())
        assert PASSWORD not in dump
        assert response.json()["access_token"] not in dump
        assert response.json()["refresh_token"] not in dump


def test_signup_request_shape_errors_are_422_validation_error_BASE_03(client):
    bodies = [
        {"email": "a@example.com", "password": PASSWORD},
        {"email": "a@example.com", "password": PASSWORD, "client_platform": "desktop"},
        {"email": "a@example.com", "password": 12345678, "client_platform": "web"},
        {"email": "a@example.com", "password": PASSWORD, "client_platform": "web", "x": 1},
        {"email": "a@example.com", "password": "a1" * 600, "client_platform": "web"},
    ]
    for body in bodies:
        response = client.post(SIGNUP, json=body)
        assert_problem(response, 422, "VALIDATION_ERROR")
        assert "a1a1a1a1" not in response.text


def test_signup_is_404_when_password_login_disabled_BASE_03(client, db, monkeypatch):
    monkeypatch.setattr(settings, "auth_password_login_enabled", False)
    assert_problem(signup(client, "off@example.com", PASSWORD), 404, "AUTH_PASSWORD_LOGIN_DISABLED")
    assert credentials(db) == []
