"""CHAT-US-01 대화 세션 생성·재개·소프트 삭제 이후 접근."""

from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import psycopg
import pytest
from psycopg.errors import CheckViolation

STORIES = "/api/v1/conversation/stories"
SESSIONS = "/api/v1/conversation/sessions"
AUTHOR = {"X-User-Id": "1"}
PLAYER = {"X-User-Id": "2"}


@pytest.fixture
def saved_story(db):
    story = db.execute(
        """INSERT INTO stories (owner_id, title, save_state)
           VALUES (1, 'Saved', 'public') RETURNING id"""
    ).fetchone()["id"]
    chars = db.execute(
        """INSERT INTO characters (story_id, name, position)
           VALUES (%s, 'Aria Moon', 0), (%s, 'Bea', 1) RETURNING id""",
        (story, story),
    ).fetchall()
    first, second = (row["id"] for row in chars)
    intros = db.execute(
        """INSERT INTO intros (story_id, title, position)
           VALUES (%s, 'Default', 0), (%s, 'Selected', 1) RETURNING id""",
        (story, story),
    ).fetchall()
    default, selected = (row["id"] for row in intros)
    db.execute(
        """INSERT INTO intro_messages (intro_id, speaker_kind, character_id, body, position)
           VALUES (%s, 'user', NULL, 'Hello, **{{user}}** / {{User}}', 2),
                  (%s, 'character', %s, 'Welcome {{user}}', 1),
                  (%s, 'narrator', NULL, 'Default only', 0)""",
        (selected, selected, first, default),
    )
    db.execute(
        """INSERT INTO intro_scene_settings (intro_id, place, time_format, hour_input, minute_input,
                                             meridiem)
           VALUES (%s, 'Other place', '24h', NULL, NULL, NULL),
                  (%s, 'Library', '12h', '12', '00', 'am')""",
        (default, selected),
    )
    db.execute(
        """INSERT INTO intro_character_states VALUES
               (%s, %s, 'Hat', NULL), (%s, %s, 'Coat', 'Hidden')""",
        (default, first, selected, first),
    )
    db.execute(
        """INSERT INTO character_initial_affinities
               (source_character_id, target_kind, target_character_id, affinity_input)
           VALUES (%s, 'user', NULL, '0'), (%s, 'character', %s, '-5')""",
        (first, second, first),
    )
    db.execute(
        """INSERT INTO character_initial_relationships
               (source_kind, source_character_id, target_kind, target_character_id, label)
           VALUES ('user', NULL, 'character', %s, 'Ally'),
                  ('character', %s, 'character', %s, 'Rival')""",
        (second, first, second),
    )
    image = db.execute(
        """INSERT INTO images (owner_id, storage_key, mime_type, byte_size)
           VALUES (1, 'sample', 'image/png', 1) RETURNING id"""
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO story_user_presets (story_id, name, image_id, description, position)
           VALUES (%s, '{{user}}', %s, 'Profile', 0), (%s, '{{User}}', %s, NULL, 1)""",
        (story, image, story, image),
    )
    db.execute("INSERT INTO user_play_settings VALUES (1, 'Author'), (2, 'Player')")
    db.execute(
        """INSERT INTO user_profiles (owner_id, name, image_id)
           VALUES (1, 'Author profile', %s), (2, 'Player profile', %s)""",
        (image, image),
    )
    return story, default, selected


def start(client, story, intro, headers=PLAYER, **fields):
    return client.post(
        f"{STORIES}/{story}/sessions",
        headers=headers,
        json={
            "intro_id": intro,
            "profile_kind": "preset",
            "profile_id": 1,
            "play_name": "",
            **fields,
        },
    )


def test_create_copies_selected_state_and_renders_snapshot(client, db, saved_story):
    story, _, intro = saved_story
    created = start(client, story, intro)
    assert created.status_code == 201
    session = created.json()["id"]
    detail = client.get(f"{SESSIONS}/{session}", headers=PLAYER).json()
    assert detail == {
        "id": session,
        "story_title": "Saved",
        "read_only": False,
        "model": None,
        "messages": [
            {"speaker_kind": "character", "speaker_name": "Aria Moon", "body": "Welcome Player"},
            {
                "speaker_kind": "user",
                "speaker_name": "Player",
                "body": "Hello, **Player** / {{User}}",
            },
        ],
        "characters": [{"id": 1, "name": "Aria Moon"}, {"id": 2, "name": "Bea"}],
    }
    assert db.execute(
        "SELECT name, description FROM conversation_user_profiles WHERE session_id = %s",
        (session,),
    ).fetchone() == {"name": "Player", "description": "Profile"}
    assert db.execute(
        """SELECT place, date_mode, year, hour, minute FROM conversation_scene_states
           WHERE session_id = %s""",
        (session,),
    ).fetchone() == {"place": "Library", "date_mode": None, "year": None, "hour": 0, "minute": 0}
    assert db.execute(
        """SELECT name_snapshot, clothing, inner_thought FROM conversation_character_states
           WHERE session_id = %s ORDER BY id""",
        (session,),
    ).fetchall() == [
        {"name_snapshot": "Aria Moon", "clothing": "Coat", "inner_thought": "Hidden"},
        {"name_snapshot": "Bea", "clothing": None, "inner_thought": None},
    ]
    assert db.execute(
        """SELECT source_session_character_id, target_session_character_id, affinity
           FROM conversation_affinities WHERE session_id = %s
           ORDER BY source_session_character_id""",
        (session,),
    ).fetchall() == [
        {"source_session_character_id": 1, "target_session_character_id": None, "affinity": 0},
        {"source_session_character_id": 2, "target_session_character_id": 1, "affinity": -5},
    ]
    assert db.execute(
        """SELECT source_kind, label FROM conversation_relationships
           WHERE session_id = %s ORDER BY label""",
        (session,),
    ).fetchall() == [
        {"source_kind": "user", "label": "Ally"},
        {"source_kind": "character", "label": "Rival"},
    ]
    assert (
        db.execute(
            "SELECT body FROM intro_messages WHERE intro_id = %s AND position = 1",
            (intro,),
        ).fetchone()["body"]
        == "Welcome {{user}}"
    )


def test_sessions_are_isolated_and_resume_returns_latest_owned(client, db, saved_story):
    story, default, selected = saved_story
    older = start(client, story, default).json()["id"]
    assert db.execute(
        "SELECT hour, minute FROM conversation_scene_states WHERE session_id = %s",
        (older,),
    ).fetchone() == {"hour": None, "minute": None}
    db.execute("UPDATE conversation_affinities SET affinity = 99 WHERE session_id = %s", (older,))
    newer = start(client, story, selected, play_name="Custom").json()["id"]
    unrelated = start(client, story, default, headers=AUTHOR).json()["id"]
    assert len({older, newer, unrelated}) == 3
    assert client.get(f"{STORIES}/{story}", headers=PLAYER).json()["existing_session_id"] == newer
    assert (
        client.get(f"{STORIES}/{story}", headers=AUTHOR).json()["existing_session_id"] == unrelated
    )
    assert client.get(f"{SESSIONS}/{newer}", headers=AUTHOR).status_code == 404
    assert client.get(f"{SESSIONS}/{newer}", headers=PLAYER).json()["messages"][0]["body"] == (
        "Welcome Custom"
    )
    assert (
        db.execute(
            """SELECT affinity FROM conversation_affinities
               WHERE session_id = %s AND target_kind = 'user'""",
            (newer,),
        ).fetchone()["affinity"]
        == 0
    )


@pytest.mark.parametrize("delete_intro", [False, True])
def test_identical_start_retry_reuses_original_session(client, db, saved_story, delete_intro):
    story, _, intro = saved_story
    first = start(client, story, intro)
    assert first.status_code == 201

    # 응답을 못 받은 사이 원본이 편집되어도 세션과 초기 상태는 이미 저장되어 있다.
    if delete_intro:
        db.execute("DELETE FROM intros WHERE id = %s", (intro,))
    retry = start(client, story, intro)
    assert retry.status_code == 201
    assert retry.json() == {"id": first.json()["id"]}
    assert db.execute("SELECT id FROM conversation_sessions").fetchall() == [first.json()]


def test_identical_start_retry_requires_current_story_access(client, db, saved_story):
    story, _, intro = saved_story
    first = start(client, story, intro)
    assert first.status_code == 201
    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (story,))

    retry = start(client, story, intro)
    assert retry.status_code == 404
    assert "id" not in retry.json()
    assert db.execute("SELECT id FROM conversation_sessions").fetchall() == [first.json()]


def test_different_start_payloads_can_resolve_to_same_play_name(client, saved_story):
    story, _, intro = saved_story
    default = start(client, story, intro)
    explicit = start(client, story, intro, play_name="Player")
    assert default.status_code == explicit.status_code == 201
    assert default.json()["id"] != explicit.json()["id"]


def test_concurrent_identical_starts_create_one_session(client, db, db_url, saved_story):
    story, _, intro = saved_story
    with ThreadPoolExecutor(max_workers=2) as workers:
        with psycopg.connect(db_url) as gate:
            gate.execute("SELECT id FROM stories WHERE id = %s FOR UPDATE", (story,))
            requests = [workers.submit(start, client, story, intro) for _ in range(2)]
            deadline = monotonic() + 5
            while True:
                waiting = db.execute(
                    """SELECT count(*) AS n FROM pg_stat_activity
                       WHERE datname = current_database() AND wait_event_type = 'Lock'
                         AND query LIKE '%FROM stories%FOR UPDATE%'"""
                ).fetchone()["n"]
                if waiting == 2:
                    break
                assert monotonic() < deadline, "두 요청이 스토리 잠금에 도달하지 않았습니다."
                sleep(0.01)
        first, second = (request.result(timeout=5) for request in requests)

    assert first.status_code == second.status_code == 201
    assert second.json() == {"id": first.json()["id"]}
    assert db.execute("SELECT id FROM conversation_sessions").fetchall() == [first.json()]


def test_ac07_deleted_story_preserves_each_owners_read_only_snapshot(client, db, saved_story):
    story, _, intro = saved_story
    author_session = start(client, story, intro, headers=AUTHOR).json()["id"]
    player_session = start(client, story, intro).json()["id"]
    db.execute("DELETE FROM intros WHERE id = %s", (intro,))
    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (story,))
    detail = client.get(f"{SESSIONS}/{author_session}", headers=AUTHOR).json()
    assert detail["read_only"] is True
    assert detail["messages"][0]["body"] == "Welcome Author"
    assert client.get(f"{SESSIONS}/{author_session}", headers=PLAYER).status_code == 404
    player_detail = client.get(f"{SESSIONS}/{player_session}", headers=PLAYER)
    assert player_detail.status_code == 200
    assert player_detail.json()["read_only"] is True
    assert player_detail.json()["messages"][0]["body"] == "Welcome Player"
    assert client.get(f"{SESSIONS}/{player_session}", headers=AUTHOR).status_code == 404
    assert client.get(f"{STORIES}/{story}", headers=AUTHOR).status_code == 404
    assert start(client, story, intro, headers=AUTHOR).status_code == 404


@pytest.mark.parametrize(
    "fields",
    [
        {"intro_id": 999},
        {"profile_id": 999},
        {"profile_kind": "personal", "profile_id": 1},
        {"play_name": "ariAMoon"},
        {"play_name": " Ari "},
        {"play_name": "{{user}}"},
        {"play_name": " "},
        {"play_name": "a" * 1000},
        {"profile_id": 2},
    ],
)
def test_invalid_selection_or_play_name_creates_no_session(client, db, saved_story, fields):
    story, _, intro = saved_story
    assert start(client, story, intro, **fields).status_code == 422
    assert db.execute("SELECT count(*) AS n FROM conversation_sessions").fetchone()["n"] == 0


def test_nul_play_name_is_rejected_at_request_validation(client, db, saved_story):
    story, _, intro = saved_story
    response = start(client, story, intro, play_name="Before\0After")
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["code"] == "VALIDATION_ERROR"
    assert [error["loc"] for error in problem["errors"]] == [["body", "play_name"]]
    assert "Before" not in response.text
    assert db.execute("SELECT count(*) AS n FROM conversation_sessions").fetchone()["n"] == 0


def test_personal_selection_copies_only_own_profile(client, db, saved_story):
    story, _, intro = saved_story
    created = start(client, story, intro, profile_kind="personal", profile_id=2, play_name="New")
    assert created.status_code == 201
    assert db.execute(
        "SELECT source_preset_id, source_profile_id, name FROM conversation_user_profiles"
    ).fetchone() == {"source_preset_id": None, "source_profile_id": 2, "name": "New"}
    assert db.execute("SELECT name FROM user_profiles WHERE id = 2").fetchone()["name"] == (
        "Player profile"
    )


def test_create_rolls_back_if_state_copy_fails(client, db, saved_story):
    story, _, intro = saved_story
    db.execute("ALTER TABLE conversation_scene_states ADD CHECK (place <> 'Library')")
    try:
        with pytest.raises(CheckViolation, match="conversation_scene_states"):
            start(client, story, intro)
        assert db.execute("SELECT count(*) AS n FROM conversation_sessions").fetchone()["n"] == 0
        assert (
            db.execute("SELECT count(*) AS n FROM conversation_user_profiles").fetchone()["n"] == 0
        )
    finally:
        db.execute(
            "ALTER TABLE conversation_scene_states "
            "DROP CONSTRAINT conversation_scene_states_place_check"
        )
