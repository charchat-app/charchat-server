"""CHAT-US-01 배포 SQL 재실행 및 스냅샷이 없는 기존 방 조회 회귀 검증."""

from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[4] / "app/db/migrations/kan16_conversation_sessions.sql"
)
STORIES = "/api/v1/conversation/stories"
SESSIONS = "/api/v1/conversation/sessions"
PLAYER = {"X-User-Id": "2"}


def seed_legacy_rooms(db):
    # KAN-16 이전 스키마에서도 이 데이터 준비를 그대로 실행할 수 있다.
    with db.transaction():
        story = db.execute(
            "INSERT INTO stories (owner_id, title, save_state) "
            "VALUES (1, 'Synthetic story', 'public') RETURNING id"
        ).fetchone()["id"]
        kept_intro, deleted_intro = (
            row["id"]
            for row in db.execute(
                "INSERT INTO intros (story_id, title, position) "
                "VALUES (%s, 'Edited', 0), (%s, 'Deleted', 1) RETURNING id",
                (story, story),
            )
        )
        db.execute(
            "INSERT INTO intro_messages (intro_id, speaker_kind, body, position) "
            "VALUES (%s, 'narrator', 'Old text', 0), (%s, 'narrator', 'Deleted text', 0)",
            (kept_intro, deleted_intro),
        )
        edited_room, deleted_room = (
            row["id"]
            for row in db.execute(
                "INSERT INTO conversation_sessions (owner_id, story_id, source_intro_id) "
                "VALUES (2, %s, %s), (2, %s, %s) RETURNING id",
                (story, kept_intro, story, deleted_intro),
            )
        )
        image = db.execute(
            "INSERT INTO images (owner_id, storage_key, mime_type, byte_size) "
            "VALUES (2, 'synthetic-image', 'image/png', 1) RETURNING id"
        ).fetchone()["id"]
        profile = db.execute(
            "INSERT INTO user_profiles (owner_id, name, image_id) "
            "VALUES (2, 'Player', %s) RETURNING id",
            (image,),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO conversation_user_profiles "
            "(session_id, source_kind, source_profile_id, name, image_id) "
            "VALUES (%s, 'personal', %s, 'Player', %s), (%s, 'personal', %s, 'Player', %s)",
            (edited_room, profile, image, deleted_room, profile, image),
        )
        db.execute(
            "UPDATE intro_messages SET body = 'Edited for {{user}}' WHERE intro_id = %s",
            (kept_intro,),
        )
        db.execute("DELETE FROM intros WHERE id = %s", (deleted_intro,))
    return story, kept_intro, profile, edited_room, deleted_room


def assert_legacy_rooms(db, client, kept_intro, edited_room, deleted_room):
    rows = db.execute(
        "SELECT source_intro_id, intro_messages_snapshot, initial_request_fingerprint "
        "FROM conversation_sessions WHERE id IN (%s, %s) ORDER BY id",
        (edited_room, deleted_room),
    ).fetchall()
    assert rows == [
        {
            "source_intro_id": source_intro_id,
            "intro_messages_snapshot": [],
            "initial_request_fingerprint": None,
        }
        for source_intro_id in (kept_intro, None)
    ]
    for room in (edited_room, deleted_room):
        response = client.get(f"{SESSIONS}/{room}", headers=PLAYER)
        assert response.status_code == 200
        assert response.json()["messages"] == []


def create_and_assert_new_room(db, client, story, intro, profile):
    request = {
        "intro_id": intro,
        "profile_kind": "personal",
        "profile_id": profile,
        "play_name": "Player",
    }
    created = client.post(f"{STORIES}/{story}/sessions", headers=PLAYER, json=request)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    retry = client.post(f"{STORIES}/{story}/sessions", headers=PLAYER, json=request)
    assert retry.status_code == 201
    assert retry.json()["id"] == session_id
    row = db.execute(
        "SELECT intro_messages_snapshot, initial_request_fingerprint "
        "FROM conversation_sessions WHERE id = %s",
        (session_id,),
    ).fetchone()
    assert len(row["initial_request_fingerprint"]) == 32
    assert row["intro_messages_snapshot"] == [
        {"speaker_kind": "narrator", "speaker_name": None, "body": "Edited for Player"}
    ]
    return session_id, row


def test_migration_rerun_preserves_legacy_rooms_and_captured_data(db, client):
    story, intro, profile, edited_room, deleted_room = seed_legacy_rooms(db)
    migration = MIGRATION.read_text(encoding="utf-8")
    db.execute(migration)
    assert_legacy_rooms(db, client, intro, edited_room, deleted_room)
    session_id, captured = create_and_assert_new_room(db, client, story, intro, profile)
    db.execute("DELETE FROM intros WHERE id = %s", (intro,))

    db.execute(migration)

    assert_legacy_rooms(db, client, None, edited_room, deleted_room)
    response = client.get(f"{SESSIONS}/{session_id}", headers=PLAYER)
    assert response.status_code == 200
    assert response.json()["messages"] == captured["intro_messages_snapshot"]
    assert (
        db.execute(
            "SELECT intro_messages_snapshot, initial_request_fingerprint "
            "FROM conversation_sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
        == captured
    )
