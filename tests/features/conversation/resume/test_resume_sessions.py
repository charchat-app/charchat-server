"""CHAT-US-03 내 대화 목록에서 기존 대화방 찾기·다시 열기."""

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from resume_helpers import AUTHOR, PLAYER, SESSIONS, count, seed_story, send, start

from app.main import create_app


def _brief(page):
    return [
        {key: row[key] for key in ("id", "story_title", "read_only", "play_name")}
        for row in page["sessions"]
    ]


def _hide_source(db, story_id, title):
    # 현재 제작 정책에서 금지된 공개→비공개 상태는 테스트 데이터로만 재현한다.
    with db.transaction():
        db.execute("ALTER TABLE stories DISABLE TRIGGER stories_lifecycle_guard")
        db.execute(
            "UPDATE stories SET save_state = 'private', title = %s WHERE id = %s",
            (title, story_id),
        )
        db.execute("ALTER TABLE stories ENABLE TRIGGER stories_lifecycle_guard")


def test_chat_us_03_ac01_ac02_lists_owned_rooms_and_reopens_selected_snapshot(db, db_url, story):
    with TestClient(create_app(db_url)) as writer:
        older = start(writer, story, play_name="Older")
        newer = start(writer, story, play_name="Newer")
        other = start(writer, story, headers=AUTHOR, play_name="작가")
        assert send(writer, older, "첫 대사").status_code == 202
    db.execute("UPDATE intro_messages SET body = 'Edited after start'")
    sessions_before = count(db, "conversation_sessions")

    # 읽기 전용 DB 연결로 목록과 상세에서 쓰기를 시도하지 않는지 확인한다.
    read_url = make_conninfo(db_url, options="-c default_transaction_read_only=on")
    with TestClient(create_app(read_url)) as reader:
        listed = reader.get(SESSIONS, headers=PLAYER)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["next_before_id"] is None
        assert _brief(body) == [
            {"id": newer, "story_title": "비 오는 도시", "read_only": False, "play_name": "Newer"},
            {"id": older, "story_title": "비 오는 도시", "read_only": False, "play_name": "Older"},
        ]
        newer_row, older_row = body["sessions"]
        assert newer_row["model"] == older_row["model"] == "루나"
        assert newer_row["generating"] is older_row["generating"] is False
        assert newer_row["last_message_preview"] is None
        assert newer_row["last_activity_at"] is None
        # 마지막 완료 메시지(가짜 공급자의 응답)만 미리보기로 쓴다.
        assert older_row["last_message_preview"].startswith("(가짜 응답)")
        assert older_row["last_activity_at"] is not None

        selected = body["sessions"][1]["id"]
        room = reader.get(f"{SESSIONS}/{selected}", headers=PLAYER)
        assert room.status_code == 200
        assert room.json()["id"] == older
        assert [m["body"] for m in room.json()["messages"]] == ["비가 내린다.", "왔어, Older?"]
        assert room.json()["model"] == "루나"
        assert reader.get(f"{SESSIONS}/{other}", headers=PLAYER).status_code == 404
    # 목록 조회·다시 열기는 새 세션을 만들지 않는다(AC02).
    assert count(db, "conversation_sessions") == sessions_before


def test_chat_us_03_ac01_preview_uses_only_completed_messages(client, db, story, monkeypatch):
    from app.features.conversation.messaging import worker

    session = start(client, story)
    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    assert send(client, session, "  보낸   대사\n둘째 줄  ").status_code == 202
    db.execute(
        """UPDATE conversation_generations SET status = 'streaming', text = '검증 전 원문'"""
    )
    row = client.get(SESSIONS, headers=PLAYER).json()["sessions"][0]
    # 생성 중 원문은 목록에 나오지 않고, 저장된 유저 메시지만 공백을 줄여 보인다.
    assert row["last_message_preview"] == "보낸 대사 둘째 줄"
    assert "검증 전" not in str(row)
    assert row["generating"] is True

    long_text = "가" * 100
    db.execute("UPDATE conversation_generations SET status = 'failed'")
    db.execute(
        "UPDATE conversation_message_parts SET body = %s WHERE speaker_kind = 'user'",
        (long_text,),
    )
    row = client.get(SESSIONS, headers=PLAYER).json()["sessions"][0]
    assert row["last_message_preview"] == "가" * 80 + "…"
    assert row["generating"] is False


def test_chat_us_03_ac01_ac02_follows_current_room_access_policy(client, db):
    source = seed_story(db, "Formerly public")
    player_room = start(client, source)
    author_room = start(client, source, headers=AUTHOR, play_name="작가")
    _hide_source(db, source[0], "Private edit")

    assert client.get(SESSIONS, headers=PLAYER).json() == {"sessions": [], "next_before_id": None}
    denied = client.get(f"{SESSIONS}/{player_room}", headers=PLAYER)
    assert denied.status_code == 404
    assert "Private edit" not in denied.text
    author_page = client.get(SESSIONS, headers=AUTHOR).json()
    assert _brief(author_page) == [
        {"id": author_room, "story_title": "Private edit", "read_only": False, "play_name": "작가"}
    ]

    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (source[0],))
    deleted_page = client.get(SESSIONS, headers=PLAYER).json()
    assert _brief(deleted_page) == [
        {"id": player_room, "story_title": "Private edit", "read_only": True, "play_name": "민서"}
    ]
    room = client.get(f"{SESSIONS}/{player_room}", headers=PLAYER)
    assert room.status_code == 200
    assert room.json()["read_only"] is True
    assert client.get(f"{SESSIONS}/{author_room}", headers=PLAYER).status_code == 404


def test_chat_us_03_ac01_pages_visible_rooms_by_exclusive_id(client, db):
    public = seed_story(db, None)
    hidden = seed_story(db, "Private metadata")
    oldest = start(client, public, play_name="Oldest")
    start(client, hidden)
    visible = [start(client, public, play_name=f"Player {i}") for i in range(30)]
    start(client, public, headers=AUTHOR, play_name="작가")
    _hide_source(db, hidden[0], "Private metadata")

    first = client.get(SESSIONS, headers=PLAYER).json()
    assert [row["id"] for row in first["sessions"]] == list(reversed(visible))
    assert {row["story_title"] for row in first["sessions"]} == {""}
    assert first["next_before_id"] == visible[0]
    start(client, public, play_name="Added after page one")
    second = client.get(SESSIONS, headers=PLAYER, params={"before_id": first["next_before_id"]})
    assert second.status_code == 200
    assert [row["id"] for row in second.json()["sessions"]] == [oldest]
    assert second.json()["next_before_id"] is None
    empty = client.get(SESSIONS, headers=PLAYER, params={"before_id": oldest})
    assert empty.json() == {"sessions": [], "next_before_id": None}

    # 정확히 30개가 남으면 다음 페이지가 없다.
    exact = client.get(SESSIONS, headers=PLAYER, params={"before_id": visible[-1]}).json()
    assert [row["id"] for row in exact["sessions"]] == [*reversed(visible[:-1]), oldest]
    assert exact["next_before_id"] is None

    high = client.get(SESSIONS, headers=PLAYER, params={"before_id": 2**63})
    assert high.status_code == 200
    assert [r["id"] for r in high.json()["sessions"]] == [
        r["id"] for r in client.get(SESSIONS, headers=PLAYER).json()["sessions"]
    ]


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
