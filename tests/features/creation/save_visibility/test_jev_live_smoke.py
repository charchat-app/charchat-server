"""CREATE-US-08-AC02·AC04 Jev 종단 스모크 흐름(가짜 Jev로 오프라인 확인).

`run_smoke`·`assert_approved`는 실제 호출 개발자 러너(`jev_live_runner.py`)도 쓴다.
이 파일은 실제 호출·API 키·환경변수를 다루지 않는다.

고정된 중립 합성 성인 데이터(명시적 32세, 유저와 가족 관계 없음)만 쓴다.
POST는 최대 한 번이며 자동 재시도하지 않는다.
기록은 허용 목록 값(HTTP 상태·소요 시간·결과 코드)뿐이다.
공급자 응답 원문·헤더·자격 증명은 기록·출력하지 않는다.
"""

import time
from types import SimpleNamespace

import httpx
import psycopg
from fastapi.testclient import TestClient

from app.features.creation.save_visibility.jev_decisions import ENDPOINT, JevDecisionsEvaluator
from app.features.creation.save_visibility.rating_review import (
    DatabaseReviewResults,
    RatingReviewCoordinator,
)
from app.main import create_app
from tests.features.creation.images.helpers import give_face_images
from tests.features.creation.save_visibility.jev_http_transport import (
    DEFAULT_DEADLINE_SECONDS,
    JevHttpTransport,
)
from tests.features.creation.save_visibility.test_jev_decisions import encode, valid_answers
from tests.features.creation.save_visibility.test_jev_http_transport import streamed
from tests.features.creation.speech_style.test_common_save_api import body, current_payload, put

OWNER_ID = 7001
OTHER_USER_ID = 7002  # 스토리와 무관한 이용자(조회 권한 확인용)
# 고정 중립 합성 데이터. 실제 인물·서비스 데이터가 아니다.
STORY = {
    "title": "가상 시립 도서관",
    "subtitle": "조용한 도서관의 하루",
    "world": (
        "평범한 현대 도시의 시립 도서관이다. 유저는 책을 빌리러 처음 방문한 손님이며, "
        "사서 윤서와는 가족이나 친척 관계가 아닌 처음 만난 사이다."
    ),
}
CHARACTER = {
    "name": "윤서",
    "description": (
        "서른두 살(32세) 성인 사서. 이 도서관에서 칠 년째 일하고 있으며 "
        "고전 소설 추천을 좋아한다. 유저와는 가족 관계가 아니다."
    ),
}
_ALLOWED_RECORD_KEYS = ("posts", "status", "elapsed_ms", "transport", "outcome")


class SinglePost:
    """실제 POST를 최대 한 번으로 막고 허용 목록 값(상태·소요 시간·전송 결과)만 기록한다."""

    def __init__(self, inner):
        self._inner = inner
        self.record = {"posts": 0, "status": None, "elapsed_ms": None, "transport": None}

    def send(self, request):
        if self.record["posts"] >= 1:
            raise RuntimeError("두 번째 POST 차단")  # 재시도 없음
        self.record["posts"] += 1
        started = time.monotonic()
        try:
            reply = self._inner.send(request)
        except Exception:
            self.record["transport"] = "transport_failure"
            raise
        finally:
            self.record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        self.record["status"] = reply.status if type(reply.status) is int else None
        self.record["transport"] = "ok"
        return reply


def _create_adult_story(dsn):
    with psycopg.connect(dsn, autocommit=True) as connection:
        story = connection.execute(
            """INSERT INTO stories(owner_id,title,subtitle,world_description)
               VALUES (%s,%s,%s,%s) RETURNING id,saved_at""",
            (OWNER_ID, STORY["title"], STORY["subtitle"], STORY["world"]),
        ).fetchone()
        story = {"id": story[0], "saved_at": story[1]}
        connection.execute(
            "INSERT INTO characters(story_id,name,description,position) VALUES (%s,%s,%s,0)",
            (story["id"], CHARACTER["name"], CHARACTER["description"]),
        )
        give_face_images(connection, story["id"])
    return story


def _stored_rating(dsn, story):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """SELECT requested_rating, effective_rating, rating_review_status,
                      rating_review_reason IS NULL
               FROM stories WHERE id=%s""",
            (story["id"],),
        ).fetchone()


def _rating_view(client, story, user_id=OWNER_ID):
    response = client.get(
        f"/api/v1/creation/stories/{story['id']}/creation", headers={"X-User-Id": str(user_id)}
    )
    if response.status_code != 200:
        return response.status_code
    data = response.json()
    return tuple(data[k] for k in ("effective_rating", "rating_review_status"))


def _report(record):
    """허용 목록 값만 한 줄로 남긴다(`-s`로 볼 수 있다)."""
    print("JEV_LIVE_SMOKE " + " ".join(f"{k}={record.get(k)}" for k in _ALLOWED_RECORD_KEYS))


def run_smoke(db_url, transport):
    """저장 API로 공개 저장 → UNSAFE 요청 → 검토 대기 → 새 앱으로 조회 → DB 확인."""
    spy = SinglePost(transport)
    coordinator = RatingReviewCoordinator(
        JevDecisionsEvaluator(spy), results=DatabaseReviewResults(db_url)
    )
    record = spy.record
    try:
        with TestClient(create_app(db_url, rating_review=coordinator)) as client:
            client.headers["X-User-Id"] = str(OWNER_ID)
            story = _create_adult_story(db_url)

            published = put(client, story, body("private"), "live-publish")
            assert published.status_code == 200, published.status_code
            assert coordinator.wait(10)
            assert record["posts"] == 0  # SAFE 저장은 보내지 않는다.

            requested = put(
                client,
                story,
                {**current_payload(published.json()), "requested_rating": "UNSAFE"},
                "live-unsafe",
            )
            assert requested.status_code == 200, requested.status_code
            assert coordinator.wait(DEFAULT_DEADLINE_SECONDS + 15)
    finally:
        coordinator.close()

    # 새 앱(조정기 없음)으로 다시 조회해 저장된 결과를 본다.
    with TestClient(create_app(db_url)) as fresh:
        view = _rating_view(fresh, story)
        other = _rating_view(fresh, story, OTHER_USER_ID)
    stored = _stored_rating(db_url, story)
    record["outcome"] = f"{view[1]}:{view[0]}" if isinstance(view, tuple) else f"http_{view}"
    _report(record)
    return record, view, other, stored


def assert_approved(record, view, other, stored):
    assert record["posts"] == 1, record["posts"]
    assert record["transport"] == "ok", record["transport"]
    assert record["status"] == 200, record["status"]
    assert view == ("UNSAFE", "completed"), record["outcome"]
    assert stored == ("UNSAFE", "UNSAFE", "completed", True), stored
    assert other == 404, other  # 스토리와 무관한 이용자는 조회할 수 없다.


def test_smoke_flow_offline_with_mock_jev_CREATE_US_08_AC02_AC04(db, db_url):
    """실제 호출 전에 같은 흐름(HTTP 전송 포함)을 가짜 Jev로 확인한다(네트워크 없음)."""
    handler = []

    def respond(request):
        handler.append(str(request.url))
        decisions = SimpleNamespace(body=request.content)
        content = encode({"answers": valid_answers(decisions)})
        return streamed(content)

    transport = JevHttpTransport(
        "sk-or-synthetic-offline", deadline=5, transport=httpx.MockTransport(respond)
    )
    assert_approved(*run_smoke(db_url, transport))
    assert handler == [ENDPOINT]
