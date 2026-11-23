"""CHAT-US-03 세션 목록에서 기존 대화방 다시 열기."""

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app.main import create_app

SESSIONS = "/api/v1/conversation/sessions"
AUTHOR = {"X-User-Id": "1"}
PLAYER = {"X-User-Id": "2"}


def _saved_story(db, title: str | None = "Saved story"):
    story = db.execute(
        "INSERT INTO stories (owner_id, title, save_state) VALUES (1, %s, 'public') RETURNING id",
        (title,),
    ).fetchone()["id"]
    intro = db.execute(
        "INSERT INTO intros (story_id, position) VALUES (%s, 0) RETURNING id", (story,)
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO intro_messages (intro_id, speaker_kind, body, position)
           VALUES (%s, 'user', 'Welcome {{user}}', 0)""",
        (intro,),
    )
    profile = db.execute(
        """WITH image AS (
               INSERT INTO images (owner_id, storage_key, mime_type, byte_size)
               VALUES (1, %s, 'image/png', 1) RETURNING id
           )
           INSERT INTO story_user_presets (story_id, name, image_id, position)
           SELECT %s, '{{user}}', id, 0 FROM image RETURNING id""",
        (f"story-{story}", story),
    ).fetchone()["id"]
    return story, intro, profile


def _start(client, saved_story, *, headers=PLAYER, play_name="Player"):
    story, intro, profile = saved_story
    response = client.post(
        f"/api/v1/conversation/stories/{story}/sessions",
        headers=headers,
        json={
            "intro_id": intro,
            "profile_kind": "preset",
            "profile_id": profile,
            "play_name": play_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _seed_private_source_with_existing_rooms(db, story_id, title):
    # 현재 제작 정책에서 금지된 공개→비공개 상태는 테스트 데이터로만 재현한다.
    with db.transaction():
        db.execute("ALTER TABLE stories DISABLE TRIGGER stories_lifecycle_guard")
        db.execute(
            "UPDATE stories SET save_state = 'private', title = %s WHERE id = %s",
            (title, story_id),
        )
        db.execute("ALTER TABLE stories ENABLE TRIGGER stories_lifecycle_guard")


def test_chat_us_03_ac01_ac02_lists_owned_rooms_and_reopens_selected_snapshot(db, db_url):
    story = _saved_story(db)
    with TestClient(create_app(db_url)) as writer:
        older = _start(writer, story, play_name="Older")
        newer = _start(writer, story, play_name="Newer")
        other = _start(writer, story, headers=AUTHOR)
    db.execute("UPDATE intro_messages SET body = 'Edited after start'")

    # 읽기 전용 DB 연결로 목록과 상세에서 쓰기를 시도하지 않는지 확인한다.
    read_url = make_conninfo(db_url, options="-c default_transaction_read_only=on")
    with TestClient(create_app(read_url)) as reader:
        listed = reader.get(SESSIONS, headers=PLAYER)
        assert listed.status_code == 200, listed.text
        assert listed.json() == {
            "sessions": [
                {"id": sid, "story_id": story[0], "story_title": "Saved story", "read_only": False}
                for sid in (newer, older)
            ],
            "next_before_id": None,
        }
        selected = listed.json()["sessions"][1]["id"]
        room = reader.get(f"{SESSIONS}/{selected}", headers=PLAYER)
        assert room.status_code == 200
        assert room.json()["id"] == older
        assert room.json()["messages"] == [
            {"speaker_kind": "user", "speaker_name": "Older", "body": "Welcome Older"}
        ]
        assert room.json()["model"] is None
        assert reader.get(f"{SESSIONS}/{other}", headers=PLAYER).status_code == 404


def test_chat_us_03_ac01_ac02_follows_current_room_access_policy(client, db):
    former_public = _saved_story(db, "Formerly public")
    player_room = _start(client, former_public)
    author_room = _start(client, former_public, headers=AUTHOR)
    _seed_private_source_with_existing_rooms(db, former_public[0], "Private edit")

    assert client.get(SESSIONS, headers=PLAYER).json() == {"sessions": [], "next_before_id": None}
    denied = client.get(f"{SESSIONS}/{player_room}", headers=PLAYER)
    assert denied.status_code == 404
    assert "Private edit" not in denied.text
    author_page = client.get(SESSIONS, headers=AUTHOR)
    assert author_page.status_code == 200
    assert author_page.json()["sessions"] == [
        {
            "id": author_room,
            "story_id": former_public[0],
            "story_title": "Private edit",
            "read_only": False,
        }
    ]

    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (former_public[0],))
    deleted_page = client.get(SESSIONS, headers=PLAYER)
    assert deleted_page.status_code == 200
    assert deleted_page.json() == {
        "sessions": [
            {
                "id": player_room,
                "story_id": former_public[0],
                "story_title": "Private edit",
                "read_only": True,
            }
        ],
        "next_before_id": None,
    }
    room = client.get(f"{SESSIONS}/{player_room}", headers=PLAYER)
    assert room.status_code == 200
    assert room.json()["read_only"] is True
    assert room.json()["messages"][0]["body"] == "Welcome Player"
    assert client.get(f"{SESSIONS}/{author_room}", headers=PLAYER).status_code == 404


def test_chat_us_03_ac01_pages_visible_rooms_by_exclusive_id(client, db):
    public = _saved_story(db, None)
    hidden = _saved_story(db, "Private metadata")
    oldest = _start(client, public, play_name="Oldest")
    _start(client, hidden)
    visible = [_start(client, public, play_name=f"Player {i}") for i in range(30)]
    _start(client, public, headers=AUTHOR)
    _seed_private_source_with_existing_rooms(db, hidden[0], "Private metadata")

    first = client.get(SESSIONS, headers=PLAYER)
    assert first.status_code == 200
    assert first.json() == {
        "sessions": [
            {"id": sid, "story_id": public[0], "story_title": "", "read_only": False}
            for sid in reversed(visible)
        ],
        "next_before_id": visible[0],
    }
    _start(client, public, play_name="Added after page one")
    second = client.get(
        SESSIONS, headers=PLAYER, params={"before_id": first.json()["next_before_id"]}
    )
    assert second.status_code == 200
    assert second.json() == {
        "sessions": [{"id": oldest, "story_id": public[0], "story_title": "", "read_only": False}],
        "next_before_id": None,
    }
    empty = client.get(SESSIONS, headers=PLAYER, params={"before_id": oldest})
    assert empty.status_code == 200
    assert empty.json() == {"sessions": [], "next_before_id": None}

    # 정확히 30개가 남으면 숨겨진 행을 다음 페이지로 계산하지 않는다.
    exact = client.get(SESSIONS, headers=PLAYER, params={"before_id": visible[-1]})
    assert exact.status_code == 200
    assert [row["id"] for row in exact.json()["sessions"]] == [*reversed(visible[:-1]), oldest]
    assert exact.json()["next_before_id"] is None

    high = client.get(SESSIONS, headers=PLAYER, params={"before_id": 2**63})
    assert high.status_code == 200
    assert high.json() == client.get(SESSIONS, headers=PLAYER).json()


@pytest.mark.parametrize("cursor", ["0", "-1", "bad", "1.5", ""])
def test_chat_us_03_ac01_rejects_invalid_cursor(client, cursor):
    response = client.get(SESSIONS, headers=PLAYER, params={"before_id": cursor})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("headers", [{}, {"X-User-Id": "0"}])
def test_chat_us_03_ac01_requires_dev_user_context(client, headers):
    response = client.get(SESSIONS, headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "UNAUTHENTICATED"
