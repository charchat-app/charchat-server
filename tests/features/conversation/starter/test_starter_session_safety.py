"""CREATE-US-08-AC15 대화 시작·재시도·세션 조회에 조합 판정기(로컬 검사 → Jev 정본) 주입(DB 필요).

로컬 검사기는 기록용 가짜이고, Jev 전송은 가짜 전송(`MarkerTransport`)이다.
실제 공급자를 부르지 않는다.
"""

from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.common_safety import CommonSafety, SafetyProfile
from app.core.session_safety import ComposedSessionSafetyGuard
from app.features.conversation.starter import repository
from app.features.creation.save_visibility.jev_story_safety import JevStorySafetyGuard
from app.main import create_app
from tests.features.conversation.starter.test_starter_common_safety import (
    PLAYER,
    SESSIONS,
    STORIES,
    seed,
    session_count,
    start,
)
from tests.features.creation.save_visibility.test_jev_story_safety_api import MarkerTransport

PLAY = "ZQX-SESSION-PLAY-1201"
PERSONAL_NAME = "ZQX-PERSONAL-NAME-2302"
PERSONAL_DESC = "ZQX-PERSONAL-DESC-3403"
EDITED_DESC = "ZQX-PERSONAL-EDITED-4504"
USER_VALUES = (PLAY, PERSONAL_NAME, PERSONAL_DESC, EDITED_DESC, "Hello " + PLAY)


class Local:
    """로컬 전용 검사기 가짜. 받은 전체 표면을 기록한다."""

    def __init__(self, outcome=CommonSafety.CLEAR, on_call=None):
        self.outcome = outcome
        self.on_call = on_call
        self.surfaces = []

    def __call__(self, surface):
        self.surfaces.append(surface)
        if self.on_call is not None:
            self.on_call(surface)
        return self.outcome


@contextmanager
def composed(db_url, local, transport):
    guard = ComposedSessionSafetyGuard(local, JevStorySafetyGuard(transport))
    with TestClient(create_app(db_url, common_safety=guard)) as c:
        yield c


def _story(db, rating="SAFE"):
    story = seed(db, "public")
    db.execute(
        "UPDATE stories SET requested_rating = %s, effective_rating = %s WHERE id = %s",
        (rating, rating, story["id"]),
    )
    return story


def _personal(db):
    image = db.execute("SELECT id FROM images LIMIT 1").fetchone()["id"]
    return db.execute(
        """INSERT INTO user_profiles (owner_id, name, image_id, description)
           VALUES (2, %s, %s, %s) RETURNING id""",
        (PERSONAL_NAME, image, PERSONAL_DESC),
    ).fetchone()["id"]


def _start_personal(client, story, profile_id):
    return client.post(
        f"{STORIES}/{story['id']}/sessions",
        headers=PLAYER,
        json={
            "intro_id": story["intro"],
            "profile_kind": "personal",
            "profile_id": profile_id,
            "play_name": PLAY,
        },
    )


def _assert_no_user_values(transport):
    for body in transport.bodies:
        assert not any(value in body for value in USER_VALUES)
        assert "{{user}}" in body  # 렌더링 전 정본만 나간다.


# --- 허용: 두 등급 모두 저장·조회 ----------------------------------------------------------


@pytest.mark.parametrize("rating", ["SAFE", "UNSAFE"])
def test_allowed_start_persists_session_and_get_for_both_ratings_CREATE_US_08_AC15(
    db, db_url, rating
):
    story = _story(db, rating)
    local, transport = Local(), MarkerTransport()
    with composed(db_url, local, transport) as client:
        created = start(client, story, play_name=PLAY)
        assert created.status_code == 201, created.text
        session = client.get(f"{SESSIONS}/{created.json()['id']}", headers=PLAYER)
    assert session.status_code == 200
    assert session.json()["messages"][0]["body"] == f"Hello {PLAY}"
    assert session_count(db) == 1
    applied = db.execute("SELECT name, description FROM conversation_user_profiles").fetchone()
    assert (applied["name"], applied["description"]) == (PLAY, "Preset")
    # 로컬 검사기는 실제 바인딩 값 전체를 본다(시작·조회).
    started, viewed = local.surfaces
    for surface in (started, viewed):
        assert surface.session_origin is True
        assert surface.bound_play_name == PLAY
        assert surface.bound_applied_profile == SafetyProfile(PLAY, "Preset")
        assert surface.bound_intro[0].body == f"Hello {PLAY}"
    assert viewed.bound_character_names == ("Aria", "Bea")
    # Jev에는 응답마다 한 번, 정본만 나간다.
    assert len(transport.bodies) == 2
    _assert_no_user_values(transport)


# --- 로컬 거부·부재: 외부 호출 없음, 행 없음 -----------------------------------------------


@pytest.mark.parametrize("outcome", [CommonSafety.BLOCKED, CommonSafety.UNAVAILABLE, "clear"])
def test_local_deny_sends_nothing_and_creates_no_row_CREATE_US_08_AC15(db, db_url, outcome):
    story = _story(db)
    local, transport = Local(outcome), MarkerTransport()
    with composed(db_url, local, transport) as client:
        created = start(client, story, play_name=PLAY)
    assert created.status_code == 404
    assert created.json()["code"] == "CONVERSATION_STORY_NOT_FOUND"
    assert session_count(db) == 0
    assert transport.bodies == []
    assert len(local.surfaces) == 1


def test_local_deny_on_session_get_sends_nothing_CREATE_US_08_AC15(db, db_url, client):
    story = _story(db)
    session_id = start(client, story, play_name=PLAY).json()["id"]  # 기본 앱(판정기 없음)
    local, transport = Local(CommonSafety.BLOCKED), MarkerTransport()
    with composed(db_url, local, transport) as guarded:
        response = guarded.get(f"{SESSIONS}/{session_id}", headers=PLAYER)
    assert response.status_code == 404
    assert response.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"
    assert PLAY not in response.text
    assert transport.bodies == []
    assert local.surfaces[0].bound_play_name == PLAY


def test_missing_local_checker_fails_closed_without_provider_CREATE_US_08_AC15(db, db_url):
    story = _story(db)
    transport = MarkerTransport()
    with composed(db_url, None, transport) as client:
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 404
        assert start(client, story, play_name=PLAY).status_code == 404
    assert session_count(db) == 0
    assert transport.bodies == []


# --- 공급자 거부·모호·장애: 행 없음 ---------------------------------------------------------


@pytest.mark.parametrize("default", ["blocked", "unclear", RuntimeError("down")])
def test_provider_denied_or_ambiguous_creates_no_row_CREATE_US_08_AC15(db, db_url, default):
    story = _story(db)
    local, transport = Local(), MarkerTransport(default)
    with composed(db_url, local, transport) as client:
        created = start(client, story, play_name=PLAY)
    assert created.status_code == 404
    assert session_count(db) == 0
    assert len(transport.bodies) == 1
    _assert_no_user_values(transport)


# --- 잘못된 시작 입력: 외부 호출 없음 -------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [{"intro_id": 999_999}, {"profile_id": 999_999}, {"play_name": " bad "}],
    ids=["intro", "profile", "play-name"],
)
def test_invalid_start_never_calls_external_CREATE_US_08_AC15(db, db_url, change):
    story = _story(db)
    body = {"intro_id": story["intro"], "profile_kind": "preset", "profile_id": 1}
    body |= {"play_name": PLAY} | change
    local, transport = Local(), MarkerTransport()
    with composed(db_url, local, transport) as client:
        created = client.post(f"{STORIES}/{story['id']}/sessions", headers=PLAYER, json=body)
    assert created.status_code == 404
    assert session_count(db) == 0
    assert transport.bodies == [] and local.surfaces == []


# --- 개인 프로필: 로컬만 보고, 외부로는 나가지 않는다 ---------------------------------------


def test_personal_profile_is_checked_locally_only_on_start_and_get_CREATE_US_08_AC15(db, db_url):
    story = _story(db)
    profile_id = _personal(db)
    local, transport = Local(), MarkerTransport()
    with composed(db_url, local, transport) as client:
        created = _start_personal(client, story, profile_id)
        assert created.status_code == 201, created.text
        session = client.get(f"{SESSIONS}/{created.json()['id']}", headers=PLAYER)
    assert session.status_code == 200
    for surface in local.surfaces:
        assert surface.bound_applied_profile == SafetyProfile(PLAY, PERSONAL_DESC)
    assert len(transport.bodies) == 2
    _assert_no_user_values(transport)


def test_private_profile_change_between_preflight_and_lock_creates_no_row_CREATE_US_08_AC15(
    db, db_url
):
    story = _story(db)
    profile_id = _personal(db)

    def edit(_surface):
        with psycopg.connect(db_url, autocommit=True) as other:
            other.execute(
                "UPDATE user_profiles SET description = %s WHERE id = %s",
                (EDITED_DESC, profile_id),
            )

    local, transport = Local(on_call=edit), MarkerTransport()
    with composed(db_url, local, transport) as client:
        created = _start_personal(client, story, profile_id)
    assert created.status_code == 404
    assert session_count(db) == 0
    _assert_no_user_values(transport)


def test_replay_uses_persisted_private_profile_not_current_source_CREATE_US_08_AC15(db, db_url):
    story = _story(db)
    profile_id = _personal(db)
    with composed(db_url, Local(), MarkerTransport()) as client:
        first = _start_personal(client, story, profile_id)
    assert first.status_code == 201, first.text
    db.execute("UPDATE user_profiles SET description = %s WHERE id = %s", (EDITED_DESC, profile_id))
    local, transport = Local(), MarkerTransport()
    with composed(db_url, local, transport) as client:
        replay = _start_personal(client, story, profile_id)
        session = client.get(f"{SESSIONS}/{first.json()['id']}", headers=PLAYER)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert session.status_code == 200
    for surface in local.surfaces:
        assert surface.bound_applied_profile == SafetyProfile(PLAY, PERSONAL_DESC)
        assert EDITED_DESC not in repr(surface)
    assert session_count(db) == 1
    _assert_no_user_values(transport)


def test_replay_fails_closed_if_persisted_snapshot_changes_mid_check_CREATE_US_08_AC15(db, db_url):
    story = _story(db)
    profile_id = _personal(db)
    with composed(db_url, Local(), MarkerTransport()) as client:
        first = _start_personal(client, story, profile_id)
    assert first.status_code == 201, first.text

    def edit(_surface):
        with psycopg.connect(db_url, autocommit=True) as other:
            other.execute("UPDATE conversation_user_profiles SET description = %s", (EDITED_DESC,))

    local, transport = Local(on_call=edit), MarkerTransport()
    with composed(db_url, local, transport) as client:
        replay = _start_personal(client, story, profile_id)
    assert replay.status_code == 404
    assert "id" not in replay.json()
    assert session_count(db) == 1
    _assert_no_user_values(transport)


# --- 잠금 후 재확인: 세션 행과 적용 프로필 행을 모두 FOR SHARE로 잠근다 ------------------------

# 다른 연결에서 행 하나를 배타 잠금으로 즉시 시도한다(NOWAIT). 공유 잠금이 있으면 바로 실패한다.
_PROBES = {
    "session": "SELECT 1 FROM conversation_sessions WHERE id = %s FOR UPDATE NOWAIT",
    "profile": "SELECT 1 FROM conversation_user_profiles WHERE session_id = %s FOR UPDATE NOWAIT",
}


def _locked(db_url, session_id) -> dict[str, bool]:
    """세션 행·적용 프로필 행이 다른 트랜잭션에 잠겨 있는가. 기다리지 않는다(마감 있음)."""
    result = {}
    with psycopg.connect(db_url, autocommit=True) as probe:
        probe.execute("SET lock_timeout = '2s'")  # NOWAIT의 보조 상한
        probe.execute("SET statement_timeout = '5s'")
        for name, query in _PROBES.items():
            try:
                assert probe.execute(query, (session_id,)).fetchone() is not None, name
            except psycopg.errors.LockNotAvailable:
                result[name] = True
            else:
                result[name] = False
    return result


def _started_session(db, db_url, story) -> int:
    with composed(db_url, Local(), MarkerTransport()) as client:
        created = start(client, story, play_name=PLAY)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_locked_recheck_query_share_locks_session_and_profile_rows_CREATE_US_08_AC15(db, db_url):
    """저장소: lock=True는 두 행을 잠그고 트랜잭션이 끝나면 푼다. lock=False는 잠그지 않는다."""
    session_id = _started_session(db, db_url, _story(db))
    assert _locked(db_url, session_id) == {"session": False, "profile": False}
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.transaction():
            assert repository.get_session_bound(conn, session_id) is not None
            assert _locked(db_url, session_id) == {"session": False, "profile": False}
        with conn.transaction():
            row = repository.get_session_bound(conn, session_id, lock=True)
            assert row is not None and row[1] == PLAY  # (스냅숏, 플레이 이름, 설명)
            assert _locked(db_url, session_id) == {"session": True, "profile": True}
        assert _locked(db_url, session_id) == {"session": False, "profile": False}


def test_replay_locked_recheck_holds_both_rows_during_endpoint_CREATE_US_08_AC15(
    db, db_url, monkeypatch
):
    """엔드포인트: 재시도의 사전 판정은 잠그지 않고, 잠금 후 재확인은 두 행을 잠근 채 비교한다."""
    story = _story(db)
    session_id = _started_session(db, db_url, story)
    real = repository.get_session_bound
    calls = []

    def spy(conn, sid, *, lock=False):
        before = _locked(db_url, sid)
        row = real(conn, sid, lock=lock)
        calls.append({"lock": lock, "before": before, "after": _locked(db_url, sid)})
        return row

    monkeypatch.setattr(repository, "get_session_bound", spy)
    local, transport = Local(), MarkerTransport()
    with composed(db_url, local, transport) as client:
        replay = start(client, story, play_name=PLAY)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == session_id
    unlocked = {"session": False, "profile": False}
    assert calls == [
        {"lock": False, "before": unlocked, "after": unlocked},  # 사전 판정(읽기 전용)
        {"lock": True, "before": unlocked, "after": {"session": True, "profile": True}},
    ]
    assert _locked(db_url, session_id) == unlocked  # 응답 뒤 잠금이 남지 않는다.
    assert session_count(db) == 1
    _assert_no_user_values(transport)


def test_viewer_preview_with_composed_guard_sends_canonical_once_CREATE_US_08_AC15(db, db_url):
    story = _story(db)
    _personal(db)
    local, transport = Local(), MarkerTransport()
    with composed(db_url, local, transport) as client:
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
    assert len(local.surfaces) == 1 and local.surfaces[0].session_origin is False
    assert len(transport.bodies) == 1
    _assert_no_user_values(transport)
