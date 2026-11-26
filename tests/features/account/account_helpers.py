"""BASE-03 계정·로그인 테스트 공용 도우미(픽스처 아님)."""

import time

import jwt
from fastapi.testclient import TestClient

from app.core.config import settings

TOKENS = "/api/v1/auth/tokens"
SIGNUP = "/api/v1/auth/signup"
PROVIDERS = "/api/v1/auth/providers"
CURRENT_SESSION = "/api/v1/auth/sessions/current"
ME = "/api/v1/me"
STORIES = "/api/v1/creation/stories"
CONVERSATION_SESSIONS = "/api/v1/conversation/sessions"

STORY_BODY = {
    "title": "달빛 서점",
    "subtitle": "밤에만 문을 여는 서점의 비밀",
    "world_description": "자정에만 열리는 서점이 있다.",
    "tags": ["판타지"],
    "characters": [{"name": "서윤", "description": "서점 주인."}],
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def all_consents() -> dict[str, str]:
    return {
        "terms": settings.auth_terms_version,
        "privacy": settings.auth_privacy_version,
        "age_14_plus": "self-v1",
    }


def dev_login(client: TestClient, key: str, platform: str = "web") -> dict:
    response = client.post(
        TOKENS, json={"grant_type": "dev", "dev_user_key": key, "client_platform": platform}
    )
    assert response.status_code == 201, response.text
    return response.json()


def signup(client: TestClient, email: str, password: str = "abcd1234", platform: str = "web"):
    return client.post(
        SIGNUP, json={"email": email, "password": password, "client_platform": platform}
    )


def password_login(client: TestClient, email: str, password: str, platform: str = "web"):
    return client.post(
        TOKENS,
        json={
            "grant_type": "password",
            "email": email,
            "password": password,
            "client_platform": platform,
        },
    )


def jwt_claims(token: str) -> dict:
    """서명 확인 없이 JWT 본문을 읽는다(테스트 확인용)."""
    return jwt.decode(token, options={"verify_signature": False})


def mint_access_token(
    user_id: int | str,
    session_id: int | str,
    *,
    secret: str | None = None,
    algorithm: str = "HS256",
    issued_at: int | None = None,
    ttl: int | None = None,
    drop: tuple[str, ...] = (),
    **overrides,
) -> str:
    """서버와 같은 모양의 JWT를 직접 만든다(만료·다른 키·클레임 변조 확인용)."""
    now = int(time.time()) if issued_at is None else issued_at
    claims = {
        "iss": settings.auth_jwt_issuer,
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": now,
        "exp": now + (settings.auth_access_ttl_seconds if ttl is None else ttl),
        "typ": "access",
        **overrides,
    }
    for name in drop:
        claims.pop(name, None)
    key = settings.auth_jwt_secret.get_secret_value() if secret is None else secret
    return jwt.encode(claims, key, algorithm=algorithm)


def expired_access_token(token: str) -> str:
    """같은 계정·세션을 가리키는, 서명은 맞고 이미 만료된 JWT."""
    claims = jwt_claims(token)
    return mint_access_token(
        claims["sub"], claims["sid"], issued_at=int(time.time()) - 3600, ttl=1800
    )


def refresh(client: TestClient, refresh_token: str):
    return client.post(TOKENS, json={"grant_type": "refresh_token", "refresh_token": refresh_token})


def onboard(client: TestClient, access_token: str, name: str = "민서") -> dict:
    response = client.patch(
        ME,
        headers=bearer(access_token),
        json={"default_play_name": name, "consents": all_consents()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    return response.json()


def active_login(client: TestClient, key: str) -> dict:
    """dev 로그인 후 온보딩까지 마친 계정의 토큰 응답."""
    tokens = dev_login(client, key)
    onboard(client, tokens["access_token"])
    return tokens


def create_story(client: TestClient, access_token: str) -> int:
    response = client.post(STORIES, json=STORY_BODY, headers=bearer(access_token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def seed_conversation_session(db, owner_id: int) -> int:
    """owner_id 소유의 공개 스토리와 대화 세션 하나를 DB에 직접 만든다."""
    with db.transaction():
        story = db.execute(
            "INSERT INTO stories (owner_id, title, save_state) "
            "VALUES (%s, 'Synthetic story', 'public') RETURNING id",
            (owner_id,),
        ).fetchone()["id"]
        intro = db.execute(
            "INSERT INTO intros (story_id, title, position) VALUES (%s, 'Intro', 0) RETURNING id",
            (story,),
        ).fetchone()["id"]
        session = db.execute(
            "INSERT INTO conversation_sessions (owner_id, story_id, source_intro_id, "
            "selected_model_id) VALUES (%s, %s, %s, (SELECT id FROM models WHERE name = '루나')) "
            "RETURNING id",
            (owner_id, story, intro),
        ).fetchone()["id"]
        image = db.execute(
            "INSERT INTO images (owner_id, storage_key, mime_type, byte_size) "
            "VALUES (%s, %s, 'image/png', 1) RETURNING id",
            (owner_id, f"synthetic-{session}"),
        ).fetchone()["id"]
        profile = db.execute(
            "INSERT INTO user_profiles (owner_id, name, image_id) "
            "VALUES (%s, 'Player', %s) RETURNING id",
            (owner_id, image),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO conversation_user_profiles "
            "(session_id, source_kind, source_profile_id, name, image_id) "
            "VALUES (%s, 'personal', %s, 'Player', %s)",
            (session, profile, image),
        )
    return session
