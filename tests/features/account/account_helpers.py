"""BASE-03 계정·로그인 테스트 공용 도우미(픽스처 아님)."""

from fastapi.testclient import TestClient

from app.core.config import settings

TOKENS = "/api/v1/auth/tokens"
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
