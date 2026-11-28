"""CREATE-US-08-AC15 대화 시작 실제 호출 도우미(`jev_session_safety_live`)의 오프라인 확인.

실제 공급자·자격 증명을 쓰지 않는다. HTTP 전송은 `httpx.MockTransport`(네트워크 없음)이고,
DB 테스트는 `db` 픽스처(전용 테스트 DB)를 쓴다.
"""

import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from app.core.common_safety import CommonSafety
from app.db import session
from app.features.creation.save_visibility.jev_decisions import ENDPOINT
from app.features.creation.save_visibility.jev_http_transport import JevHttpTransport
from app.features.creation.save_visibility.jev_story_safety import QUESTION, build_request
from tests.features.creation.save_visibility import jev_session_safety_live as live
from tests.features.creation.save_visibility.jev_session_safety_live import (
    EXPECTED_SURFACE,
    PLAY_NAME,
    ExactBoundChecker,
    PersistMismatch,
    assert_start_persisted,
    expected_session_surface,
    run_start_smoke,
)
from tests.features.creation.save_visibility.jev_story_safety_live import (
    EgressMismatch,
    NotFresh,
    NotIsolated,
)
from tests.features.creation.save_visibility.test_jev_http_transport import streamed
from tests.features.creation.save_visibility.test_jev_story_safety_api import MarkerTransport

ROOT = Path(__file__).resolve().parents[4]


def _clear_body():
    answer = {
        "type": "choice",
        "choice": "clear",
        "probabilities": {"clear": 0.9, "blocked": 0.1, "unclear": 0},
    }
    return json.dumps({"answers": {QUESTION: answer}}).encode("utf-8")


def _mock_http(bodies, urls):
    def respond(request):
        urls.append(str(request.url))
        bodies.append(request.content)
        return streamed(_clear_body())

    return JevHttpTransport(
        "sk-or-synthetic-offline", deadline=5, transport=httpx.MockTransport(respond)
    )


# --- DB 없이: 모의 로컬 검사기·앱 미포함 ------------------------------------------------------


def test_exact_bound_checker_clears_only_exact_fixed_surface_CREATE_US_08_AC15():
    checker = ExactBoundChecker(41)
    assert checker(expected_session_surface(41)) is CommonSafety.CLEAR
    altered = [
        expected_session_surface(42),
        dataclasses.replace(expected_session_surface(41), bound_play_name="다른이름"),
        dataclasses.replace(expected_session_surface(41), bound_applied_profile=None),
        dataclasses.replace(expected_session_surface(41), bound_intro=()),
        dataclasses.replace(expected_session_surface(41), bound_character_names=("민준",)),
        dataclasses.replace(expected_session_surface(41), session_origin=False),
        dataclasses.replace(expected_session_surface(41), title="다른 제목"),
        dataclasses.replace(EXPECTED_SURFACE, story_id=41),  # 바인딩 값 없는 정본
        None,
    ]
    for surface in altered:
        assert checker(surface) is CommonSafety.UNAVAILABLE
    assert (checker.calls, checker.mismatches) == (1 + len(altered), len(altered))


def test_simulated_local_checker_is_not_shipped_in_app_CREATE_US_08_AC15():
    """모의 로컬 검사기와 실제 호출 도우미는 테스트 전용이다(앱 코드가 참조하지 않는다)."""
    names = ("jev_session_safety_live", "ExactBoundChecker", "jev_story_safety_live", "tests.")
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(name in text for name in names), path.name


def test_assert_start_persisted_requires_single_attempt_and_snapshot_CREATE_US_08_AC15():
    record = {"posts": 1, "status": 200, "elapsed_ms": 3, "transport": "ok", "outcome": "start_201"}
    assert_start_persisted(record, 201, 1, True)
    with pytest.raises(AssertionError):
        assert_start_persisted(record, 201, 2, True)  # 두 번째 외부 요청 시도
    with pytest.raises(PersistMismatch):
        assert_start_persisted(record, 201, 1, False)
    with pytest.raises(AssertionError):
        assert_start_persisted({**record, "outcome": "local_mismatch"}, 201, 1, True)


# --- DB 필요: 오프라인 전송으로 같은 흐름 확인 -------------------------------------------------


def test_live_start_smoke_offline_http_transport_CREATE_US_08_AC15(db):
    """HTTP 전송 계층(가짜 네트워크)까지: POST 201, 같은 연결로 저장 확인, 1회 전송."""
    bodies, urls = [], []
    record, status, attempts, persisted = run_start_smoke(db, _mock_http(bodies, urls))
    assert_start_persisted(record, status, attempts, persisted)
    assert urls == [ENDPOINT]  # 두 번째 요청 없음
    [body] = bodies
    # 실제로 나간 본문이 고정 리터럴로만 만든 기대 정본 요청과 바이트 단위로 같다.
    assert body == build_request(EXPECTED_SURFACE).body
    assert PLAY_NAME.encode("utf-8") not in body  # 세션 바인딩 값은 외부로 나가지 않는다.
    assert session._pool is None
    row = db.execute("SELECT name, description FROM conversation_user_profiles").fetchone()
    assert row["name"] == PLAY_NAME


def test_live_start_smoke_refuses_mismatched_egress_before_send_CREATE_US_08_AC15(db, monkeypatch):
    # 고정 요청 비교(`PinnedEgress`)의 기대 표면만 바꾼다. 로컬 모의 검사기의 기대 표면은 그대로라
    # 로컬은 통과하고, 외부 전송 직전 비교에서만 막혀야 한다.
    real_pin = live.PinnedEgress
    pins = []

    def altered_pin(inner, expected):
        pin = real_pin(inner, dataclasses.replace(expected, subtitle="다른 부제"))
        pins.append(pin)
        return pin

    monkeypatch.setattr(live, "PinnedEgress", altered_pin)
    checkers = []
    real_checker = live.ExactBoundChecker

    def recording_checker(story_id):
        checker = real_checker(story_id)
        checkers.append(checker)
        return checker

    monkeypatch.setattr(live, "ExactBoundChecker", recording_checker)
    transport = MarkerTransport()
    with pytest.raises(EgressMismatch):
        run_start_smoke(db, transport)
    [pin] = pins
    [checker] = checkers
    assert (checker.calls, checker.mismatches) == (1, 0)  # 로컬 검사는 통과했다.
    assert pin.refused == 1
    assert transport.bodies == []
    assert db.execute("SELECT count(*) AS n FROM conversation_sessions").fetchone()["n"] == 0


def test_live_start_smoke_local_mismatch_sends_nothing_and_creates_no_row_CREATE_US_08_AC15(
    db, monkeypatch
):
    monkeypatch.setattr(live, "PLAY_NAME", "다른상인")  # 요청 이름이 고정 바인딩 값과 다르다.
    transport = MarkerTransport()
    record, status, attempts, persisted = run_start_smoke(db, transport)
    assert (status, attempts, persisted) == (404, 0, False)
    assert record["outcome"] == "local_mismatch" and record["posts"] == 0
    assert transport.bodies == []
    assert db.execute("SELECT count(*) AS n FROM conversation_sessions").fetchone()["n"] == 0


def test_live_start_smoke_refuses_open_global_pool_before_writing_CREATE_US_08_AC15(
    db, monkeypatch
):
    monkeypatch.setattr(session, "_pool", object())
    transport = MarkerTransport()
    with pytest.raises(NotIsolated):
        run_start_smoke(db, transport)
    assert db.execute("SELECT count(*) AS n FROM stories").fetchone()["n"] == 0
    assert transport.bodies == []


def test_live_start_smoke_refuses_non_fresh_db_CREATE_US_08_AC15(db):
    from tests.features.conversation.starter.test_starter_common_safety import seed

    seed(db, "public")
    transport = MarkerTransport()
    with pytest.raises(NotFresh):
        run_start_smoke(db, transport)
    assert transport.bodies == []
