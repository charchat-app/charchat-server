"""CREATE-US-08-AC02·AC04 Jev 종단 스모크 흐름(가짜 Jev로 오프라인 확인).

`run_smoke`·`assert_approved`는 실제 호출 개발자 러너(`jev_live_runner.py`)도 쓴다.
이 파일은 실제 호출·API 키·환경변수를 다루지 않는다.

고정된 중립 합성 성인 데이터(명시적 32세, 유저와 가족 관계 없음)만 쓴다.
POST는 최대 한 번이며 자동 재시도하지 않는다.
기록은 허용 목록 값(HTTP 상태·소요 시간·결과 코드)뿐이다.
공급자 응답 원문·헤더·자격 증명은 기록·출력하지 않는다.
"""

import json
import time
from types import SimpleNamespace

import httpx
import psycopg
from fastapi.testclient import TestClient

from app.features.creation.save_visibility.jev_decisions import (
    ENDPOINT,
    REVISION,
    DecisionsReply,
    JevDecisionsEvaluator,
)
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
# CREATE-US-08-AC14: 이 스모크의 조정기에만 명시적으로 켜는 재사용 이름공간(고정 개정 기준).
# 기본 앱(`create_app` 기본값)에는 켜지 않는다.
REUSE_NAMESPACE = REVISION.replace("/", "-")


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


class Attempts:
    """바깥 전송 시도 수만 센다(차단된 시도 포함). 값·원문은 보관하지 않는다."""

    def __init__(self, inner):
        self._inner = inner
        self.count = 0

    def send(self, request):
        self.count += 1
        return self._inner.send(request)


class PinnedRevision:
    """응답 `model`이 고정 개정(`REVISION`)일 때만 그대로 넘긴다.

    별칭·누락·다른 개정·해석 불가 응답은 답 없는 본문으로 바꿔 `unavailable`로 닫는다
    (재사용 이름공간에 다른 개정의 통과 지문이 남지 않게). 원문은 보관하지 않는다.
    """

    def __init__(self, inner):
        self._inner = inner

    def send(self, request):
        reply = self._inner.send(request)
        try:
            model = json.loads(reply.body).get("model")
        except (AttributeError, TypeError, ValueError):
            model = None
        if model != REVISION:
            return DecisionsReply(reply.status, b"{}")
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


def _coordinator(db_url, spy):
    """재사용 이름공간을 명시적으로 켠 조정기(고정 개정 응답만 통과로 남긴다)."""
    return RatingReviewCoordinator(
        JevDecisionsEvaluator(PinnedRevision(spy)),
        results=DatabaseReviewResults(db_url),
        reuse_namespace=REUSE_NAMESPACE,
    )


def _save_view(response):
    if response.status_code != 200:
        return response.status_code
    data = response.json()
    return tuple(data[k] for k in ("effective_rating", "rating_review_status"))


def run_smoke(db_url, transport):
    """저장 API로 공개 저장 → UNSAFE 요청 → 검토 대기 → 조정기 종료 →
    새 조정기(같은 DB)로 같은 내용 재저장(AC14 재사용) → 새 앱(조정기 없음)으로 조회 → DB 확인.
    """
    spy = SinglePost(transport)
    attempts = Attempts(spy)
    record = spy.record
    unsafe = None
    coordinator = _coordinator(db_url, attempts)
    try:
        with TestClient(create_app(db_url, rating_review=coordinator)) as client:
            client.headers["X-User-Id"] = str(OWNER_ID)
            story = _create_adult_story(db_url)

            published = put(client, story, body("private"), "live-publish")
            assert published.status_code == 200, published.status_code
            assert coordinator.wait(10)
            assert record["posts"] == 0  # SAFE 저장은 보내지 않는다.

            unsafe = {**current_payload(published.json()), "requested_rating": "UNSAFE"}
            requested = put(client, story, unsafe, "live-unsafe")
            assert requested.status_code == 200, requested.status_code
            assert coordinator.wait(DEFAULT_DEADLINE_SECONDS + 15)
    finally:
        coordinator.close()
    first = _stored_rating(db_url, story)

    # CREATE-US-08-AC14: 첫 조정기를 닫은 뒤 새 조정기(같은 DB)로 같은 본문을 실제로 다시 저장한다.
    # 같은 내용의 영속 통과를 재사용해야 하므로 바깥 시도는 늘지 않아야 한다.
    restarted = _coordinator(db_url, attempts)
    try:
        with TestClient(create_app(db_url, rating_review=restarted)) as client:
            client.headers["X-User-Id"] = str(OWNER_ID)
            again = _save_view(put(client, story, unsafe, "live-unsafe-again"))
            assert restarted.wait(10)
    finally:
        restarted.close()

    # 새 앱(조정기 없음)으로 다시 조회해 저장된 결과를 본다.
    with TestClient(create_app(db_url)) as fresh:
        view = _rating_view(fresh, story)
        other = _rating_view(fresh, story, OTHER_USER_ID)
    stored = _stored_rating(db_url, story)
    record["outcome"] = f"{view[1]}:{view[0]}" if isinstance(view, tuple) else f"http_{view}"
    _report(record)
    return record, view, other, stored, first, again, attempts.count


def assert_approved(record, view, other, stored, first, again, attempts):
    assert attempts == 1, attempts  # 재저장까지 바깥 시도는 정확히 한 번(차단된 시도 포함)
    assert record["posts"] == 1, record["posts"]
    assert record["transport"] == "ok", record["transport"]
    assert record["status"] == 200, record["status"]
    assert first == ("UNSAFE", "UNSAFE", "completed", True), first
    assert again == ("UNSAFE", "completed"), again  # 재사용으로 즉시 완료
    assert view == ("UNSAFE", "completed"), record["outcome"]
    assert stored == first, stored  # 완료 결과는 바뀌지 않는다.
    assert other == 404, other  # 스토리와 무관한 이용자는 조회할 수 없다.


def test_smoke_flow_offline_with_mock_jev_CREATE_US_08_AC02_AC04_AC14(db, db_url):
    """HTTP 전송과 재시작 재사용을 가짜 Jev로 확인한다(네트워크 없음)."""
    handler = []

    def respond(request):
        handler.append(str(request.url))
        decisions = SimpleNamespace(body=request.content)
        content = encode({"model": REVISION, "answers": valid_answers(decisions)})
        return streamed(content)

    transport = JevHttpTransport(
        "sk-or-synthetic-offline", deadline=5, transport=httpx.MockTransport(respond)
    )
    assert_approved(*run_smoke(db_url, transport))
    assert handler == [ENDPOINT]


def test_pinned_revision_passes_only_pinned_model_CREATE_US_08_AC14():
    """별칭·누락·다른 개정·해석 불가 응답은 통과 지문이 남지 않게 답 없는 본문으로 닫는다."""
    from app.features.creation.save_visibility.jev_decisions import MODEL

    class Fixed:
        def __init__(self, reply):
            self.reply = reply

        def send(self, request):
            return self.reply

    pinned = DecisionsReply(200, encode({"model": REVISION, "answers": {}}))
    assert PinnedRevision(Fixed(pinned)).send(None) is pinned
    for raw in (
        encode({"model": MODEL, "answers": {}}),
        encode({"answers": {}}),
        encode({"model": "typesafe/jev-1.14-20991231", "answers": {}}),
        encode([REVISION]),
        b"\xff",
    ):
        assert PinnedRevision(Fixed(DecisionsReply(200, raw))).send(None) == DecisionsReply(
            200, b"{}"
        )
