"""CREATE-US-08-AC15 Jev 스토리 공통 안전 후보의 관람 요청 1회 스모크 도우미.

pytest 수집 대상이 아니며 CI에서 실행하지 않는다.
실제 호출은 `jev_live_runner --story-safety`의 격리 자식에서만 부른다.
- 호출자가 실제(또는 가짜) 전송을 주입한다. 이 모듈은 자격 증명·환경변수를 다루지 않는다.
- 호출자는 새 일회용 DB(스키마만 있고 스토리·개인 프로필·세션이 없는 상태)를 넘긴다.
  하나라도 있으면 쓰지 않고 멈춘다.
- 호출자가 대상 확인을 마친 연결 객체 하나(dict_row·자동 커밋)를 넘긴다. 새 DB 확인·시드·앱
  요청 커넥션(의존성 재정의)이 모두 이 같은 연결을 쓴다. DSN으로 다시 연결하지 않는다.
  앱 수명 주기(전역 풀 열기)를 돌리지 않고, 전역 풀이 이미 열려 있으면 쓰기 전에 멈춘다.
- 보내기 전에 요청 전체를 고정 리터럴로만 만든 기대 표면(`EXPECTED_SURFACE`)의
  `build_request` 결과와 비교한다(DB에서 기대값을 만들지 않는다). 다르면 보내지 않는다.
- 테스트와 같은 직접 DB 시드로 중립 합성 아동 일상 스토리(성적 내용 없음)를 넣고, 관람자
  미리보기 요청을 한 번만 보낸다. POST는 최대 한 번이며 재시도하지 않는다.
- 기록은 허용 목록 값(POST 수·HTTP 상태·소요 시간·전송 결과·관람 응답 상태)뿐이다.
"""

from fastapi.testclient import TestClient

from app.core.common_safety import (
    SafetyCharacter,
    SafetyIntro,
    SafetyMessage,
    SafetyProfile,
    SafetyRelationship,
    SafetySurface,
)
from app.db import session
from app.features.creation.save_visibility.jev_decisions import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    DecisionsRequest,
)
from app.features.creation.save_visibility.jev_story_safety import (
    JevStorySafetyGuard,
    build_request,
)
from app.main import create_app
from tests.features.conversation.starter.test_starter_common_safety import PLAYER, STORIES, seed
from tests.features.creation.save_visibility.test_jev_live_smoke import SinglePost

# 고정 중립 합성 데이터(성적 내용 없는 아동 일상). 실제 인물·서비스 데이터가 아니다.
BENIGN_YOUTH = {
    "title": "시골 장터의 작은 탐정",
    "subtitle": "잃어버린 강아지 찾기",
    "world": (
        "평범한 시골 마을의 주말 장터다. 열두 살 초등학생 민준이 할머니와 장을 보다가 "
        "길 잃은 강아지를 발견하고 주인을 찾아 나선다. 유저는 장터에서 꽃을 파는 상인이다."
    ),
    "characters": (
        ("민준", "열두 살 초등학생. 호기심이 많고 동물을 좋아한다."),
        ("순자 할머니", "민준의 할머니. 장터 단골이며 상인들과 잘 안다."),
    ),
    "labels": ("손자와 할머니", "장터에서 처음 만난 사이"),
    "intro": "{{user}} 아저씨, 혹시 이 강아지 주인 보셨어요?",
    "preset": "장터에서 꽃을 파는 상인",
}


# 시드 뒤 관람 미리보기가 보낼 정본 표면. 위 고정 리터럴과 시드(`seed`)의 고정 리터럴
# ("Opening"·프리셋 이름 "{{user}}")만으로 직접 쓴다. DB에서 읽은 값으로 만들지 않는다.
# `story_id`는 요청 본문에 실리지 않는다.
EXPECTED_SURFACE = SafetySurface(
    story_id=0,
    title="시골 장터의 작은 탐정",
    subtitle="잃어버린 강아지 찾기",
    world_description=(
        "평범한 시골 마을의 주말 장터다. 열두 살 초등학생 민준이 할머니와 장을 보다가 "
        "길 잃은 강아지를 발견하고 주인을 찾아 나선다. 유저는 장터에서 꽃을 파는 상인이다."
    ),
    tags=(),
    characters=(
        SafetyCharacter("민준", "열두 살 초등학생. 호기심이 많고 동물을 좋아한다."),
        SafetyCharacter("순자 할머니", "민준의 할머니. 장터 단골이며 상인들과 잘 안다."),
    ),
    relationships=(
        SafetyRelationship("민준", "순자 할머니", "손자와 할머니"),
        SafetyRelationship("{{user}}", "순자 할머니", "장터에서 처음 만난 사이"),
    ),
    intros=(
        SafetyIntro(
            "Opening",
            (SafetyMessage("character", "민준", "{{user}} 아저씨, 혹시 이 강아지 주인 보셨어요?"),),
        ),
    ),
    bound_profiles=(SafetyProfile("{{user}}", "장터에서 꽃을 파는 상인"),),
)


class EgressMismatch(Exception):
    """보내려는 요청이 고정 기대 요청과 다르다(값을 담지 않는다)."""


class PinnedEgress:
    """고정 기대 요청과 정확히 같은 요청만 안쪽 전송으로 넘긴다. 다르면 보내지 않고 막는다."""

    def __init__(self, inner, expected: SafetySurface):
        self._inner = inner
        self._expected = build_request(expected)
        self.refused = 0

    def send(self, request):
        expected = self._expected
        if (
            expected is None
            or type(request) is not DecisionsRequest
            or request.url != ENDPOINT
            or request.max_response_bytes != MAX_RESPONSE_BYTES
            or request != expected
        ):
            self.refused += 1
            raise EgressMismatch
        return self._inner.send(request)


class NotFresh(Exception):
    """대상 DB가 새 일회용 상태가 아니다(값을 담지 않는다)."""


class NotIsolated(Exception):
    """프로세스 전역 DB 풀이 이미 열려 있다(값을 담지 않는다)."""


# 새 DB 확인 대상: 시드가 쓰는 표와 플레이어 개인 정보가 들어갈 표.
_FRESH_TABLES = ("stories", "user_profiles", "user_play_settings", "conversation_sessions")


def _assert_fresh(db):
    for table in _FRESH_TABLES:
        if db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] != 0:
            raise NotFresh


def scoped_app(conn, guard):
    """요청 커넥션으로 넘겨받은 연결 객체 하나만 쓰는 앱. 전역 풀(`get_conn`)을 쓰지 않는다.

    연결은 호출자가 닫는다. 수명 주기를 돌리지 않도록 `TestClient`를 `with` 없이 쓴다.
    """

    def scoped_conn():
        yield conn

    app = create_app(None, common_safety=guard)
    app.dependency_overrides[session.get_conn] = scoped_conn
    return app


def seed_benign_youth_story(db) -> dict:
    """테스트 시드(`seed`)에 중립 아동 일상 텍스트를 덮어쓴다. `db`는 dict_row 자동 커밋 연결."""
    story = seed(db, "public")
    data = BENIGN_YOUTH
    db.execute(
        "UPDATE stories SET title = %s, subtitle = %s, world_description = %s WHERE id = %s",
        (data["title"], data["subtitle"], data["world"], story["id"]),
    )
    for position, (name, description) in enumerate(data["characters"]):
        db.execute(
            """UPDATE characters SET name = %s, description = %s
               WHERE story_id = %s AND position = %s""",
            (name, description, story["id"], position),
        )
    rival, ally = data["labels"]
    db.execute(
        "UPDATE character_initial_relationships SET label = %s WHERE label = 'Rival label'",
        (rival,),
    )
    db.execute(
        "UPDATE character_initial_relationships SET label = %s WHERE label = 'Ally label'",
        (ally,),
    )
    db.execute(
        "UPDATE intro_messages SET body = %s WHERE intro_id = %s", (data["intro"], story["intro"])
    )
    db.execute(
        "UPDATE story_user_presets SET description = %s WHERE story_id = %s",
        (data["preset"], story["id"]),
    )
    return story


def run_viewer_smoke(db, transport):
    """확인된 연결 `db`에 시드 → 같은 연결로 후보 판정기를 주입한 앱의 관람 미리보기 1회.

    `db`는 dict_row 자동 커밋 연결이다. 전송 순서: 판정기 → 고정 요청 비교 → POST 1회 제한 →
    `transport`. 비교가 어긋나면 보내지 않고 `EgressMismatch`를 낸다.
    """
    if session._pool is not None:
        raise NotIsolated
    spy = SinglePost(transport)
    pin = PinnedEgress(spy, EXPECTED_SURFACE)
    _assert_fresh(db)
    story = seed_benign_youth_story(db)
    client = TestClient(scoped_app(db, JevStorySafetyGuard(pin)))  # 수명 주기 없음(풀 안 엶)
    response = client.get(f"{STORIES}/{story['id']}", headers=PLAYER)
    if pin.refused or session._pool is not None:
        raise EgressMismatch if pin.refused else NotIsolated
    record = dict(spy.record)
    record["outcome"] = f"view_{response.status_code}"
    return record, response.status_code


def assert_viewer_clear(record, view_status):
    assert record["posts"] == 1, record["posts"]
    assert record["transport"] == "ok", record["transport"]
    assert record["status"] == 200, record["status"]
    assert view_status == 200, record["outcome"]
