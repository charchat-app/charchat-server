"""CREATE-US-08-AC15 관람자 스토리 조회·대화 시작·세션 조회의 공통 안전 판정(주입 시에만).

판정기는 tests/common_safety_fakes.py의 중립 가짜다. 실제 공급자를 부르지 않는다.
"""

from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.common_safety import CommonSafety, SafetyProfile
from app.main import create_app
from tests.common_safety_fakes import BLOCK_MARKER, FakeGuard

STORIES = "/api/v1/conversation/stories"
SESSIONS = "/api/v1/conversation/sessions"
CREATION = "/api/v1/creation/stories"
AUTHOR = {"X-User-Id": "1"}
PLAYER = {"X-User-Id": "2"}


@contextmanager
def guarded(db_url, guard):
    with TestClient(create_app(db_url, common_safety=guard)) as c:
        yield c


@pytest.fixture
def story(db):
    return seed(db, "public")


def seed(db, save_state):
    story = db.execute(
        """INSERT INTO stories (owner_id, title, subtitle, world_description, save_state)
           VALUES (1, 'Title', 'Sub', 'World text', %s) RETURNING id""",
        (save_state,),
    ).fetchone()["id"]
    first, second = (
        row["id"]
        for row in db.execute(
            """INSERT INTO characters (story_id, name, description, position)
               VALUES (%s, 'Aria', 'Guard', 0), (%s, 'Bea', 'Scout', 1) RETURNING id""",
            (story, story),
        ).fetchall()
    )
    db.execute(
        """INSERT INTO character_initial_relationships
               (source_kind, source_character_id, target_kind, target_character_id, label)
           VALUES ('character', %s, 'character', %s, 'Rival label'),
                  ('user', NULL, 'character', %s, 'Ally label')""",
        (first, second, second),
    )
    intro = db.execute(
        "INSERT INTO intros (story_id, title, position) VALUES (%s, 'Opening', 0) RETURNING id",
        (story,),
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO intro_messages (intro_id, speaker_kind, character_id, body, position)
           VALUES (%s, 'character', %s, 'Hello {{user}}', 0)""",
        (intro, first),
    )
    image = db.execute(
        """INSERT INTO images (owner_id, storage_key, mime_type, byte_size)
           VALUES (1, 'k', 'image/png', 1) RETURNING id"""
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO story_user_presets (story_id, name, image_id, description, position)
           VALUES (%s, '{{user}}', %s, 'Preset', 0)""",
        (story, image),
    )
    db.execute("INSERT INTO user_play_settings VALUES (1, 'Author'), (2, 'Player')")
    return {"id": story, "intro": intro}


def start(client, story, headers=PLAYER, play_name=""):
    return client.post(
        f"{STORIES}/{story['id']}/sessions",
        headers=headers,
        json={
            "intro_id": story["intro"],
            "profile_kind": "preset",
            "profile_id": 1,
            "play_name": play_name,
        },
    )


def session_count(db) -> int:
    return db.execute("SELECT count(*) AS n FROM conversation_sessions").fetchone()["n"]


def mark_intro(db, story, text=BLOCK_MARKER):
    db.execute("UPDATE intro_messages SET body = %s WHERE intro_id = %s", (text, story["intro"]))


# --- 중립 허용: 두 등급 모두 진행 -----------------------------------------------------------


@pytest.mark.parametrize("rating", ["SAFE", "UNSAFE"])
def test_neutral_clear_allows_view_start_and_session_for_both_ratings_CREATE_US_08_AC15(
    db, db_url, story, rating
):
    db.execute(
        "UPDATE stories SET requested_rating = %s, effective_rating = %s WHERE id = %s",
        (rating, rating, story["id"]),
    )
    guard = FakeGuard()
    with guarded(db_url, guard) as client:
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
        created = start(client, story)
        assert created.status_code == 201, created.text
        session = client.get(f"{SESSIONS}/{created.json()['id']}", headers=PLAYER)
        assert session.status_code == 200
        assert session.json()["messages"][0]["body"] == "Hello Player"
    assert session_count(db) == 1


# --- 판정기가 보는 정본 범위 --------------------------------------------------------------


def test_evaluator_sees_canonical_and_response_bound_text_CREATE_US_08_AC15(db, db_url, story):
    guard = FakeGuard()
    with guarded(db_url, guard) as client:
        client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
        created = start(client, story)
        client.get(f"{SESSIONS}/{created.json()['id']}", headers=PLAYER)
    view, *starts, session = guard.surfaces
    for surface in (view, *starts, session):
        assert (surface.title, surface.subtitle, surface.world_description) == (
            "Title",
            "Sub",
            "World text",
        )
        assert [(c.name, c.description) for c in surface.characters] == [
            ("Aria", "Guard"),
            ("Bea", "Scout"),
        ]
        assert {(r.source, r.target, r.label) for r in surface.relationships} == {
            ("Aria", "Bea", "Rival label"),
            ("{{user}}", "Bea", "Ally label"),
        }
        assert surface.intros[0].messages[0].body == "Hello {{user}}"
    # 시작·세션 조회는 실제 응답에 묶이는 렌더링된 인트로까지 판정한다.
    assert all(s.bound_intro[0].body == "Hello Player" for s in starts)
    assert session.bound_intro[0].body == "Hello Player"
    assert session.bound_character_names == ("Aria", "Bea")


@pytest.mark.parametrize(
    "mark",
    [
        "UPDATE stories SET world_description = %(m)s WHERE id = %(s)s",
        "UPDATE stories SET subtitle = %(m)s WHERE id = %(s)s",
        "UPDATE characters SET description = %(m)s WHERE story_id = %(s)s",
        "UPDATE character_initial_relationships SET label = %(m)s",
        "UPDATE intros SET title = %(m)s WHERE story_id = %(s)s",
    ],
)
def test_marker_in_any_canonical_field_blocks_viewer_CREATE_US_08_AC15(db, db_url, story, mark):
    db.execute(mark, {"m": BLOCK_MARKER, "s": story["id"]})
    with guarded(db_url, FakeGuard()) as client:
        response = client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
        assert response.status_code == 404
        assert response.json()["code"] == "CONVERSATION_STORY_NOT_FOUND"
        assert BLOCK_MARKER not in response.text
        created = start(client, story)
        assert created.status_code == 404
        assert BLOCK_MARKER not in created.text
    assert session_count(db) == 0


def test_rendered_play_name_is_evaluated_CREATE_US_08_AC15(db, db_url, story):
    """원문은 중립이어도 실제 응답에 묶이는 렌더링 결과가 차단이면 시작하지 않는다."""
    with guarded(db_url, FakeGuard()) as client:
        created = start(client, story, play_name=BLOCK_MARKER)
        assert created.status_code == 404
        assert BLOCK_MARKER not in created.text
    assert session_count(db) == 0


# --- 응답에 묶이는 프리셋·플레이 이름 (각 틈마다 구별되는 중립 표식) -----------------------

PRESET_NAME_SENTINEL = "ZQX-PRESET-NAME-4410"
PRESET_DESC_SENTINEL = "ZQX-PRESET-DESC-5521"
PERSONAL_SENTINEL = "ZQX-PERSONAL-6632"
PLAY_NAME_SENTINEL = "ZQX-PLAYNAME-7743"
DEFAULT_NAME_SENTINEL = "ZQX-DEFAULTNAME-8854"


@pytest.mark.parametrize(
    ("column", "sentinel"),
    [("name", PRESET_NAME_SENTINEL), ("description", PRESET_DESC_SENTINEL)],
)
def test_viewer_story_evaluates_returned_preset_text_CREATE_US_08_AC15(
    db, db_url, story, column, sentinel
):
    """관람 응답 profiles에 나가는 제작자 프리셋 이름·설명도 같은 스냅숏으로 판정한다."""
    db.execute(f"UPDATE story_user_presets SET {column} = %s", (sentinel,))
    guard = FakeGuard(marker=sentinel)
    with guarded(db_url, guard) as client:
        response = client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
    assert response.status_code == 404
    assert sentinel not in response.text
    assert any(sentinel in text for text in guard.texts())


def test_viewer_story_does_not_send_unreturned_personal_profiles_CREATE_US_08_AC15(
    db, db_url, story
):
    """관람자 본인의 개인 프로필은 제작자 정본이 아니므로 판정기에 넘기지 않는다."""
    image = db.execute("SELECT id FROM images LIMIT 1").fetchone()["id"]
    db.execute(
        "INSERT INTO user_profiles (owner_id, name, image_id, description) VALUES (2, %s, %s, %s)",
        (PERSONAL_SENTINEL, image, PERSONAL_SENTINEL),
    )
    guard = FakeGuard(marker=PERSONAL_SENTINEL)
    with guarded(db_url, guard) as client:
        response = client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
    assert response.status_code == 200
    assert not any(PERSONAL_SENTINEL in text for text in guard.texts())
    assert [s.bound_profiles for s in guard.surfaces] == [
        (SafetyProfile(name="{{user}}", description="Preset"),)
    ]


def test_play_name_is_evaluated_without_intro_replacement_CREATE_US_08_AC15(db, db_url, story):
    """인트로에 {{user}}·유저 발화가 없어도 응답에 묶이는 실제 플레이 이름을 판정한다."""
    mark_intro(db, story, "Hello there")
    guard = FakeGuard(marker=PLAY_NAME_SENTINEL)
    with guarded(db_url, guard) as client:
        created = start(client, story, play_name=PLAY_NAME_SENTINEL)
    assert created.status_code == 404
    assert PLAY_NAME_SENTINEL not in created.text
    assert any(s.bound_play_name == PLAY_NAME_SENTINEL for s in guard.surfaces)
    assert session_count(db) == 0


def test_session_get_evaluates_persisted_play_name_CREATE_US_08_AC15(db, db_url, story):
    mark_intro(db, story, "Hello there")
    with guarded(db_url, FakeGuard()) as client:
        session_id = start(client, story, play_name=PLAY_NAME_SENTINEL).json()["id"]
    guard = FakeGuard(marker=PLAY_NAME_SENTINEL)
    with guarded(db_url, guard) as client:
        response = client.get(f"{SESSIONS}/{session_id}", headers=PLAYER)
    assert response.status_code == 404
    assert response.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"
    assert PLAY_NAME_SENTINEL not in response.text
    assert guard.surfaces[-1].bound_play_name == PLAY_NAME_SENTINEL


def test_idempotent_replay_evaluates_persisted_play_name_CREATE_US_08_AC15(db, db_url, story):
    """재시도는 현재 기본 이름이 아니라 기존 세션에 저장된 플레이 이름으로 판정한다."""
    mark_intro(db, story, "Hello there")
    db.execute(
        "UPDATE user_play_settings SET default_play_name = %s WHERE owner_id = 2",
        (DEFAULT_NAME_SENTINEL,),
    )
    with guarded(db_url, FakeGuard()) as client:
        first = start(client, story)  # play_name="" → 기본 이름(표식) 사용
    assert first.status_code == 201, first.text
    db.execute("UPDATE user_play_settings SET default_play_name = 'Neutral' WHERE owner_id = 2")
    guard = FakeGuard(marker=DEFAULT_NAME_SENTINEL)
    with guarded(db_url, guard) as client:
        replay = start(client, story)
    assert replay.status_code == 404
    assert replay.json()["code"] == "CONVERSATION_STORY_NOT_FOUND"
    assert "id" not in replay.json()
    assert DEFAULT_NAME_SENTINEL not in replay.text
    assert any(s.bound_play_name == DEFAULT_NAME_SENTINEL for s in guard.surfaces)
    assert session_count(db) == 1


# --- 차단·판정 불가는 fail closed ---------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [CommonSafety.BLOCKED, CommonSafety.UNAVAILABLE, RuntimeError("down"), "clear"],
)
def test_blocked_or_unavailable_fails_closed_everywhere_CREATE_US_08_AC15(
    db, db_url, story, outcome
):
    with guarded(db_url, FakeGuard()) as client:
        session_id = start(client, story).json()["id"]
    guard = FakeGuard(outcome)
    with guarded(db_url, guard) as client:
        view = client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
        retry = start(client, story)  # 같은 요청 재시도도 기존 세션 id를 주지 않는다.
        other = start(client, story, play_name="Other")
        session = client.get(f"{SESSIONS}/{session_id}", headers=PLAYER)
    assert [r.status_code for r in (view, retry, other, session)] == [404, 404, 404, 404]
    assert session.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"
    assert guard.surfaces  # 판정기를 실제로 불렀다.
    assert session_count(db) == 1


def test_private_owner_viewer_does_not_bypass_but_editor_is_unchanged_CREATE_US_08_AC15(db, db_url):
    story = seed(db, "private")
    with guarded(db_url, FakeGuard()) as client:
        # 판정 통과면 비공개 소유자는 관람 화면에서 그대로 본다(기존 접근 유지).
        assert client.get(f"{STORIES}/{story['id']}", headers=AUTHOR).status_code == 200
    mark_intro(db, story)
    with guarded(db_url, FakeGuard()) as client:
        assert client.get(f"{STORIES}/{story['id']}", headers=AUTHOR).status_code == 404
        assert start(client, story, headers=AUTHOR).status_code == 404
        # 제작자 편집 화면 접근은 바꾸지 않는다.
        assert client.get(f"{CREATION}/{story['id']}", headers=AUTHOR).status_code == 200
    assert session_count(db) == 0


def test_default_app_keeps_legacy_behavior_without_guard_CREATE_US_08_AC15(client, db, story):
    mark_intro(db, story)
    assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
    assert start(client, story).status_code == 201


# --- 수정 후 재사용 금지·단계 경계 ---------------------------------------------------------


def test_edit_is_reevaluated_without_reuse_CREATE_US_08_AC15(db, db_url, story):
    guard = FakeGuard()
    with guarded(db_url, guard) as client:
        session_id = start(client, story).json()["id"]
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
        mark_intro(db, story)
        # 같은 판정기·같은 앱이어도 이전 통과를 재사용하지 않는다.
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 404
        assert start(client, story).status_code == 404  # 기존 세션 재시도
        assert client.get(f"{SESSIONS}/{session_id}", headers=PLAYER).status_code == 404
        mark_intro(db, story, "Fixed {{user}}")
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
        assert start(client, story, play_name="Again").status_code == 201
    assert guard.surfaces[-1].bound_intro[0].body == "Fixed Again"


def test_view_response_matches_evaluated_snapshot_CREATE_US_08_AC15(db, db_url, story):
    """판정 중 저장본이 바뀌어도 응답은 판정한 스냅숏 그대로다."""

    def edit(_surface):
        with psycopg.connect(db_url, autocommit=True) as other:
            other.execute(
                "UPDATE stories SET title = %s WHERE id = %s", (BLOCK_MARKER, story["id"])
            )

    with guarded(db_url, FakeGuard(on_call=edit)) as client:
        response = client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
    assert response.status_code == 200
    assert response.json()["title"] == "Title"


def test_start_fails_closed_if_changed_between_preflight_and_save_CREATE_US_08_AC15(
    db, db_url, story
):
    """잠금 전 판정 뒤 저장본이 바뀌면(중립 문구여도) 판정하지 않은 내용으로 시작하지 않는다."""

    def edit(_surface):
        with psycopg.connect(db_url, autocommit=True) as other:
            other.execute(
                "UPDATE intro_messages SET body = 'Changed {{user}}' WHERE intro_id = %s",
                (story["intro"],),
            )

    guard = FakeGuard(on_call=edit)
    with guarded(db_url, guard) as client:
        created = start(client, story)
    assert created.status_code == 404
    assert session_count(db) == 0


# --- 세션 유래 표식(session_origin) ---------------------------------------------------------


def test_session_surfaces_are_marked_session_origin_CREATE_US_08_AC15(db, db_url, story):
    """관람 미리보기만 정본 판정이고, 시작 사전 판정(입력 오류 포함)·세션 조회는 세션 유래다."""
    guard = FakeGuard()
    with guarded(db_url, guard) as client:
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
        invalid = client.post(
            f"{STORIES}/{story['id']}/sessions",
            headers=PLAYER,
            json={
                "intro_id": story["intro"] + 999,
                "profile_kind": "preset",
                "profile_id": 1,
                "play_name": "",
            },
        )
        assert invalid.status_code == 422
        created = start(client, story)
        assert created.status_code == 201, created.text
        assert client.get(f"{SESSIONS}/{created.json()['id']}", headers=PLAYER).status_code == 200
    view, invalid_preflight, preflight, session = guard.surfaces
    assert view.session_origin is False
    assert invalid_preflight.session_origin is True
    assert invalid_preflight.bound_intro == () and invalid_preflight.bound_play_name is None
    assert preflight.session_origin is True
    assert session.session_origin is True


def test_creator_presets_are_judged_on_every_canonical_surface_CREATE_US_08_AC15(db, db_url, story):
    """관람·시작·세션 조회 모두 같은 스냅숏의 제작자 프리셋을 정본으로 판정한다."""
    image = db.execute("SELECT id FROM images LIMIT 1").fetchone()["id"]
    db.execute(
        "INSERT INTO user_profiles (owner_id, name, image_id, description) VALUES (2, %s, %s, %s)",
        (PERSONAL_SENTINEL, image, PERSONAL_SENTINEL),
    )
    guard = FakeGuard()
    with guarded(db_url, guard) as client:
        client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
        created = start(client, story)
        client.get(f"{SESSIONS}/{created.json()['id']}", headers=PLAYER)
    assert len(guard.surfaces) == 3
    for surface in guard.surfaces:
        assert surface.bound_profiles == (SafetyProfile(name="{{user}}", description="Preset"),)
    assert not any(PERSONAL_SENTINEL in text for text in guard.texts())
