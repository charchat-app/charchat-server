"""CREATE-US-08-AC15 세션 안전 조합 판정기의 대화 시작(POST) 1회 스모크 도우미.

pytest 수집 대상이 아니며 CI에서 실행하지 않는다.
실제 호출은 `jev_live_runner --session-safety`의 격리 자식에서만 부른다.
- 스토리 공통 안전 스모크(`jev_story_safety_live`)의 새 DB 확인·고정 중립 시드·고정 기대 표면
  (`EXPECTED_SURFACE`)·고정 요청 비교(`PinnedEgress`)·POST 1회 제한(`SinglePost`)·같은 연결
  앱(`scoped_app`)을 그대로 쓴다. 이 모듈은 자격 증명·환경변수를 다루지 않는다.
- 로컬 검사기는 이 모듈의 모의 검사기(`ExactBoundChecker`)다. 앱에 넣지 않는다(테스트 전용).
  표면이 고정 기대 표면 + 고정 합성 바인딩 값과 정확히 같을 때만 `CLEAR`, 아니면 판정 불가다.
- 외부(Jev)에는 조합 판정기가 정본만 넘기고, `PinnedEgress`가 그 요청을 고정 기대 요청과 비교한다.
- 대화 시작 요청을 한 번 보내 201과, 같은 연결로 저장된 세션 스냅숏을 확인한다.
  외부 전송 시도는 정확히 한 번이어야 한다(두 번째 요청 없음).
- 기록은 허용 목록 값(POST 수·HTTP 상태·소요 시간·전송 결과·시작 응답 상태)뿐이다.
"""

from dataclasses import replace

from fastapi.testclient import TestClient

from app.core.common_safety import CommonSafety, SafetyMessage, SafetyProfile
from app.core.session_safety import ComposedSessionSafetyGuard
from app.db import session
from app.features.creation.save_visibility.jev_story_safety import JevStorySafetyGuard
from tests.features.conversation.starter.test_starter_common_safety import PLAYER, STORIES
from tests.features.creation.save_visibility.jev_story_safety_live import (
    BENIGN_YOUTH,
    EXPECTED_SURFACE,
    EgressMismatch,
    NotIsolated,
    PinnedEgress,
    _assert_fresh,
    scoped_app,
    seed_benign_youth_story,
)
from tests.features.creation.save_visibility.test_jev_live_smoke import SinglePost

# 고정 중립 합성 바인딩 값(실제 이용자 값이 아니다). 프리셋 "{{user}}"를 이 이름으로 시작한다.
PLAY_NAME = "꽃상인"
BOUND_INTRO = (
    SafetyMessage("character", "민준", f"{PLAY_NAME} 아저씨, 혹시 이 강아지 주인 보셨어요?"),
)
BOUND_PROFILE = SafetyProfile(PLAY_NAME, "장터에서 꽃을 파는 상인")
# 저장되어야 하는 인트로 스냅숏(고정 리터럴).
EXPECTED_SNAPSHOT = [
    {
        "speaker_kind": "character",
        "speaker_name": "민준",
        "body": f"{PLAY_NAME} 아저씨, 혹시 이 강아지 주인 보셨어요?",
    }
]


def expected_session_surface(story_id: int):
    """로컬 검사기가 받아야 하는 전체 표면(정본 + 고정 합성 바인딩 값)."""
    return replace(
        EXPECTED_SURFACE,
        story_id=story_id,
        bound_intro=BOUND_INTRO,
        bound_character_names=(),
        bound_play_name=PLAY_NAME,
        bound_applied_profile=BOUND_PROFILE,
        session_origin=True,
    )


class ExactBoundChecker:
    """테스트 전용 모의 로컬 검사기. 정확히 고정 기대 표면일 때만 `CLEAR`. 앱에 넣지 않는다."""

    def __init__(self, story_id: int):
        self._expected = expected_session_surface(story_id)
        self.calls = 0
        self.mismatches = 0

    def __call__(self, surface):
        self.calls += 1
        if surface != self._expected:
            self.mismatches += 1
            return CommonSafety.UNAVAILABLE
        return CommonSafety.CLEAR


class CountAttempts:
    """판정기가 외부로 보내려 한 시도 수(비교·POST 제한에서 막힌 것 포함)를 센다."""

    def __init__(self, inner):
        self._inner = inner
        self.attempts = 0

    def send(self, request):
        self.attempts += 1
        return self._inner.send(request)


class PersistMismatch(Exception):
    """저장된 세션이 고정 기대값과 다르다(값을 담지 않는다)."""


def _start_body(story) -> dict:
    return {
        "intro_id": story["intro"],
        "profile_kind": "preset",
        "profile_id": story["preset"],
        "play_name": PLAY_NAME,
    }


def _persisted(db, story, session_id) -> bool:
    """같은 연결로 저장 스냅숏을 확인한다. 세션은 정확히 하나여야 한다."""
    rows = db.execute(
        """SELECT cs.id, cs.story_id, cs.owner_id, cs.source_intro_id,
                  cs.intro_messages_snapshot, up.source_kind, up.source_preset_id,
                  up.name, up.description
           FROM conversation_sessions cs
           JOIN conversation_user_profiles up ON up.session_id = cs.id"""
    ).fetchall()
    if len(rows) != 1:
        return False
    [row] = rows
    return (
        row["id"] == session_id
        and row["story_id"] == story["id"]
        and row["owner_id"] == int(PLAYER["X-User-Id"])
        and row["source_intro_id"] == story["intro"]
        and row["intro_messages_snapshot"] == EXPECTED_SNAPSHOT
        and (row["source_kind"], row["source_preset_id"]) == ("preset", story["preset"])
        and (row["name"], row["description"]) == (PLAY_NAME, BENIGN_YOUTH["preset"])
    )


def run_start_smoke(db, transport):
    """확인된 연결 `db`에 시드 → 같은 연결로 조합 판정기를 주입한 앱의 대화 시작 1회.

    `db`는 dict_row 자동 커밋 연결이다. 전송 순서: 조합 판정기(로컬 모의 검사 → 정본) →
    시도 수 → 고정 요청 비교 → POST 1회 제한 → `transport`.
    돌려주는 값: (허용 목록 기록, 시작 응답 상태, 외부 전송 시도 수, 저장 확인 여부).
    """
    if session._pool is not None:
        raise NotIsolated
    spy = SinglePost(transport)
    pin = PinnedEgress(spy, EXPECTED_SURFACE)
    counted = CountAttempts(pin)
    _assert_fresh(db)
    story = seed_benign_youth_story(db)
    [preset] = db.execute(
        "SELECT id FROM story_user_presets WHERE story_id = %s", (story["id"],)
    ).fetchall()
    story["preset"] = preset["id"]
    local = ExactBoundChecker(story["id"])
    guard = ComposedSessionSafetyGuard(local, JevStorySafetyGuard(counted))
    client = TestClient(scoped_app(db, guard))  # 수명 주기 없음(풀 안 엶)
    response = client.post(
        f"{STORIES}/{story['id']}/sessions", headers=PLAYER, json=_start_body(story)
    )
    if pin.refused or session._pool is not None:
        raise EgressMismatch if pin.refused else NotIsolated
    persisted = False
    if response.status_code == 201:
        persisted = _persisted(db, story, response.json().get("id"))
    record = dict(spy.record)
    record["outcome"] = f"start_{response.status_code}"
    if local.calls != 1 or local.mismatches:
        record["outcome"] = "local_mismatch"
    return record, response.status_code, counted.attempts, persisted


def assert_start_persisted(record, start_status, attempts, persisted):
    assert record["posts"] == 1, record["posts"]
    assert attempts == 1, attempts  # 두 번째 외부 요청 없음
    assert record["transport"] == "ok", record["transport"]
    assert record["status"] == 200, record["status"]
    assert record["outcome"] == "start_201", record["outcome"]
    assert start_status == 201, start_status
    if not persisted:
        raise PersistMismatch
