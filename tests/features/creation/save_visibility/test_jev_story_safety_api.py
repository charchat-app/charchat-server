"""CREATE-US-08-AC15 Jev 스토리 공통 안전 후보를 주입한 관람 API(가짜 전송, DB 필요).

실제 공급자를 부르지 않는다. 가짜 전송은 요청에 중립 표식이 있는지만 보고 선택을 돌려준다.
시드는 대화 시작 공통 안전 테스트의 유틸리티를 그대로 쓴다.
"""

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from app.core.common_safety import CommonSafety
from app.db import session
from app.features.creation.save_visibility.jev_decisions import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    DecisionsReply,
    DecisionsRequest,
)
from app.features.creation.save_visibility.jev_story_safety import (
    CHOICES,
    QUESTION,
    JevStorySafetyGuard,
    build_request,
)
from app.main import create_app
from tests.common_safety_fakes import BLOCK_MARKER
from tests.features.conversation.starter.test_starter_common_safety import (
    PLAYER,
    SESSIONS,
    STORIES,
    mark_intro,
    seed,
    session_count,
    start,
)
from tests.features.creation.save_visibility import jev_story_safety_live as live
from tests.features.creation.save_visibility.jev_story_safety_live import (
    EXPECTED_SURFACE,
    EgressMismatch,
    NotFresh,
    NotIsolated,
    PinnedEgress,
    assert_viewer_clear,
    run_viewer_smoke,
    scoped_app,
)

TAGS = "/api/v1/creation/tags/stories"
PERSONAL = "ZQX-PERSONAL-6632"


def _reply(choice):
    others = [c for c in CHOICES if c != choice]
    probabilities = {choice: 0.9, others[0]: 0.1, others[1]: 0}
    answer = {"type": "choice", "choice": choice, "probabilities": probabilities}
    return DecisionsReply(200, json.dumps({"answers": {QUESTION: answer}}).encode("utf-8"))


class MarkerTransport:
    """중립 표식이 있으면 blocked, 없으면 `default`. `default`가 예외면 그 예외를 낸다."""

    def __init__(self, default="clear"):
        self.default = default
        self.bodies: list[str] = []

    def send(self, request):
        raw = request.body.decode("utf-8")
        self.bodies.append(raw)
        if BLOCK_MARKER in raw:
            return _reply("blocked")
        if isinstance(self.default, BaseException):
            raise self.default
        return _reply(self.default)


def _client(db_url, transport):
    return TestClient(create_app(db_url, common_safety=JevStorySafetyGuard(transport)))


def _story(db, rating):
    story = seed(db, "public")
    db.execute(
        """UPDATE stories SET requested_rating = %s, effective_rating = %s WHERE id = %s""",
        (rating, rating, story["id"]),
    )
    tag = db.execute("INSERT INTO tags (name) VALUES ('romance') RETURNING id").fetchone()["id"]
    db.execute("INSERT INTO story_tags (story_id, tag_id) VALUES (%s, %s)", (story["id"], tag))
    return story


def _views(client, story):
    view = client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
    search = client.get(TAGS, params={"tag": "romance"}, headers=PLAYER)
    return view, search


# --- 명시적 clear·blocked·판정 불가, 두 등급 -------------------------------------------------


@pytest.mark.parametrize("rating", ["SAFE", "UNSAFE"])
@pytest.mark.parametrize(
    ("default", "marked", "visible"),
    [
        ("clear", False, True),
        ("clear", True, False),  # 표식 → 명시적 blocked
        ("blocked", False, False),
        ("unclear", False, False),
        (RuntimeError("down"), False, False),
    ],
    ids=["clear", "blocked-marker", "blocked", "unclear", "transport-error"],
)
def test_preview_and_tag_search_follow_explicit_choice_CREATE_US_08_AC15(
    db, db_url, rating, default, marked, visible
):
    story = _story(db, rating)
    if marked:
        mark_intro(db, story)
    transport = MarkerTransport(default)
    with _client(db_url, transport) as client:
        view, search = _views(client, story)
    assert view.status_code == (200 if visible else 404)
    assert search.status_code == 200
    assert [s["id"] for s in search.json()] == ([story["id"]] if visible else [])
    if not visible:
        assert view.json()["code"] == "CONVERSATION_STORY_NOT_FOUND"
    assert BLOCK_MARKER not in view.text + search.text
    assert len(transport.bodies) == 2  # 응답마다 판정(캐시 없음)


# --- 인트로만 바뀐 경우 재판정 ----------------------------------------------------------------


def test_intro_only_change_is_reevaluated_CREATE_US_08_AC15(db, db_url):
    story = _story(db, "SAFE")
    transport = MarkerTransport()
    with _client(db_url, transport) as client:
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
        mark_intro(db, story)  # 인트로 본문만 중립 표식으로 바꾼다.
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 404
        mark_intro(db, story, "Fixed {{user}}")
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
    assert len(transport.bodies) == 3
    assert "Fixed {{user}}" in transport.bodies[-1]


# --- 세션 유래 값은 보내지 않고 닫힌다 -------------------------------------------------------


def test_session_start_fails_closed_without_sending_personal_values_CREATE_US_08_AC15(db, db_url):
    """대화 시작은 렌더링 인트로·플레이 이름이 묶이므로 이 후보로는 닫힌다(조용히 통과 금지)."""
    story = _story(db, "SAFE")
    transport = MarkerTransport()
    with _client(db_url, transport) as client:
        created = start(client, story, play_name=PERSONAL)
    assert created.status_code == 404
    assert session_count(db) == 0
    assert transport.bodies == []  # 세션 유래 판정은 HTTP 없이 닫힌다.


def test_invalid_session_start_sends_nothing_CREATE_US_08_AC15(db, db_url):
    """선택 입력이 틀려 바인딩 값이 없는 사전 판정도 세션 유래라 보내지 않는다."""
    story = _story(db, "SAFE")
    transport = MarkerTransport()
    with _client(db_url, transport) as client:
        created = client.post(
            f"{STORIES}/{story['id']}/sessions",
            headers=PLAYER,
            json={
                "intro_id": story["intro"] + 999,
                "profile_kind": "preset",
                "profile_id": 1,
                "play_name": PERSONAL,
            },
        )
    assert created.status_code == 404
    assert session_count(db) == 0
    assert transport.bodies == []


def test_session_get_fails_closed_without_sending_personal_values_CREATE_US_08_AC15(
    db, db_url, client
):
    story = _story(db, "SAFE")
    created = start(client, story, play_name=PERSONAL)  # 판정기 없는 기본 앱으로 만든다.
    assert created.status_code == 201, created.text
    transport = MarkerTransport()
    with _client(db_url, transport) as guarded:
        response = guarded.get(f"{SESSIONS}/{created.json()['id']}", headers=PLAYER)
    assert response.status_code == 404
    assert response.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"
    assert transport.bodies == []


def test_preview_sends_creator_presets_but_not_viewer_profiles_CREATE_US_08_AC15(db, db_url):
    story = _story(db, "SAFE")
    image = db.execute("SELECT id FROM images LIMIT 1").fetchone()["id"]
    db.execute(
        "INSERT INTO user_profiles (owner_id, name, image_id, description) VALUES (2, %s, %s, %s)",
        (PERSONAL, image, PERSONAL),
    )
    db.execute(
        "UPDATE user_play_settings SET default_play_name = %s WHERE owner_id = 2", (PERSONAL,)
    )
    transport = MarkerTransport()
    with _client(db_url, transport) as client:
        assert client.get(f"{STORIES}/{story['id']}", headers=PLAYER).status_code == 200
    [body] = transport.bodies
    assert PERSONAL not in body
    assert json.loads(body)["state"]["creator_presets"] == [
        {"name": "{{user}}", "description": "Preset"}
    ]


def test_tag_search_sends_creator_presets_but_not_personal_profiles_CREATE_US_08_AC15(db, db_url):
    story = _story(db, "SAFE")
    image = db.execute("SELECT id FROM images LIMIT 1").fetchone()["id"]
    db.execute(
        "INSERT INTO user_profiles (owner_id, name, image_id, description) VALUES (2, %s, %s, %s)",
        (PERSONAL, image, PERSONAL),
    )
    transport = MarkerTransport()
    with _client(db_url, transport) as client:
        search = client.get(TAGS, params={"tag": "romance"}, headers=PLAYER)
    assert [s["id"] for s in search.json()] == [story["id"]]
    [body] = transport.bodies
    assert PERSONAL not in body
    assert json.loads(body)["state"]["creator_presets"] == [
        {"name": "{{user}}", "description": "Preset"}
    ]


# --- 실제 호출 도우미의 오프라인 확인 --------------------------------------------------------


def test_live_viewer_smoke_helper_offline_CREATE_US_08_AC15(db):
    transport = MarkerTransport()
    record, status = run_viewer_smoke(db, transport)
    assert_viewer_clear(record, status)
    [body] = transport.bodies
    # 실제로 나간 본문이 고정 리터럴로만 만든 기대 요청과 바이트 단위로 같다.
    assert body.encode("utf-8") == build_request(EXPECTED_SURFACE).body
    assert "열두 살" in body
    assert "Player" not in body
    assert session._pool is None  # 수명 주기를 돌리지 않아 전역 풀을 열지 않았다.


def test_live_viewer_smoke_refuses_mismatched_egress_before_send_CREATE_US_08_AC15(db, monkeypatch):
    """시드된 실제 요청이 고정 기대 요청과 다르면 아래 전송(POST 1회 제한 포함)에 닿지 않는다."""
    monkeypatch.setattr(
        live, "EXPECTED_SURFACE", dataclasses.replace(EXPECTED_SURFACE, subtitle="다른 부제")
    )
    transport = MarkerTransport()
    with pytest.raises(EgressMismatch):
        run_viewer_smoke(db, transport)
    assert transport.bodies == []


def test_live_viewer_smoke_refuses_open_global_pool_before_writing_CREATE_US_08_AC15(
    db, monkeypatch
):
    """프로세스 전역 풀이 이미 열려 있으면 다른 DB로 샐 수 있어 쓰기 전에 멈춘다."""
    monkeypatch.setattr(session, "_pool", object())
    transport = MarkerTransport()
    with pytest.raises(NotIsolated):
        run_viewer_smoke(db, transport)
    assert db.execute("SELECT count(*) AS n FROM stories").fetchone()["n"] == 0
    assert transport.bodies == []


def test_live_viewer_smoke_refuses_non_fresh_db_CREATE_US_08_AC15(db):
    seed(db, "public")
    transport = MarkerTransport()
    with pytest.raises(NotFresh):
        run_viewer_smoke(db, transport)
    assert db.execute("SELECT count(*) AS n FROM stories").fetchone()["n"] == 1
    assert transport.bodies == []


def test_live_viewer_smoke_app_reuses_the_given_connection_CREATE_US_08_AC15():
    """요청 커넥션은 넘겨받은 연결 객체 그 자체다(재연결·전역 풀 없음, 닫지 않음)."""
    conn = object()
    app = scoped_app(conn, JevStorySafetyGuard(MarkerTransport()))
    connections = app.dependency_overrides[session.get_conn]()
    assert next(connections) is conn
    connections.close()


# --- 고정 요청 비교(보내기 전) ----------------------------------------------------------------


class RecordingInner:
    def __init__(self):
        self.sent = []

    def send(self, request):
        self.sent.append(request)
        return _reply("clear")


def test_pinned_egress_forwards_only_exact_expected_request_CREATE_US_08_AC15():
    inner = RecordingInner()
    pin = PinnedEgress(inner, EXPECTED_SURFACE)
    expected = build_request(EXPECTED_SURFACE)
    assert expected.url == ENDPOINT and expected.max_response_bytes == MAX_RESPONSE_BYTES
    pin.send(build_request(EXPECTED_SURFACE))
    assert inner.sent == [expected]
    assert pin.refused == 0


def _altered(expected):
    return [
        build_request(dataclasses.replace(EXPECTED_SURFACE, title="다른 제목")),
        build_request(dataclasses.replace(EXPECTED_SURFACE, tags=("romance",))),
        dataclasses.replace(expected, url="https://example.invalid/decisions"),
        dataclasses.replace(expected, max_response_bytes=MAX_RESPONSE_BYTES + 1),
        dataclasses.replace(expected, body=expected.body + b" "),
        DecisionsRequest(ENDPOINT, b"{}", MAX_RESPONSE_BYTES),
        None,
    ]


@pytest.mark.parametrize("index", range(7))
def test_pinned_egress_refuses_mismatch_before_downstream_send_CREATE_US_08_AC15(index):
    request = _altered(build_request(EXPECTED_SURFACE))[index]
    inner = RecordingInner()
    pin = PinnedEgress(inner, EXPECTED_SURFACE)
    with pytest.raises(EgressMismatch):
        pin.send(request)
    assert inner.sent == []
    assert pin.refused == 1


def test_pinned_egress_mismatch_fails_closed_in_guard_without_send_CREATE_US_08_AC15():
    """판정기 안에서 비교가 어긋나면 판정 불가로 닫히고 아래 전송에는 아무것도 가지 않는다."""
    inner = RecordingInner()
    pin = PinnedEgress(inner, EXPECTED_SURFACE)
    other = dataclasses.replace(EXPECTED_SURFACE, world_description="다른 세계관")
    assert JevStorySafetyGuard(pin)(other) is CommonSafety.UNAVAILABLE
    assert inner.sent == [] and pin.refused == 1
