"""CREATE-US-09-AC01·AC03·AC04: 최초 등록 뒤 응답과 분리된 최초 분류. 격리 PostgreSQL만 쓴다."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db.session import get_conn
from app.features.creation.category_classification.worker import (
    ClassificationInput,
    classify_first,
)
from app.main import create_app
from tests.features.creation.speech_style.test_common_save_api import body, complete, put

STORIES = "/api/v1/creation/stories"
pytestmark = pytest.mark.usefixtures("dev_user_header_enabled")


class FakeClassifier:
    def __init__(self, result=("romance", "modern"), effect=None):
        self.result = result
        self.effect = effect
        self.calls = []

    def __call__(self, value):
        self.calls.append(value)
        if self.effect is not None:
            self.effect(value)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def api(db, db_url):
    def start(classifier=None):
        app = create_app(db_url, category_classifier=classifier)
        client = TestClient(app)
        client.__enter__()
        client.headers["X-User-Id"] = "100"
        clients.append(client)
        return client

    clients = []
    yield start
    for client in clients:
        client.__exit__(None, None, None)


def create_complete(client, db_url):
    response = client.post(STORIES, json={"title": "검수 스토리", "world_description": "마법 학교"})
    assert response.status_code == 201
    story = response.json()
    complete(db_url, story)
    return story


def state(db, story):
    return db.execute(
        """SELECT category_classification_needed AS pending,
                  ARRAY(SELECT category_code FROM story_categories WHERE story_id=s.id
                        ORDER BY category_code) AS categories
           FROM stories s WHERE id=%s""",
        (story["id"],),
    ).fetchone()


@pytest.mark.parametrize("target", ["private", "public"])
def test_first_registration_classifies_after_response_CREATE_US_09_AC01_AC04(
    api, db, db_url, target
):
    classifier = FakeClassifier()
    client = api(classifier)
    story = create_complete(client, db_url)
    response = put(client, story, body(target), "register")
    assert response.status_code == 200
    assert response.json()["save_state"] == target
    assert "categories" not in response.json()
    # AC01: 확정본의 세계관과 캐릭터 상세만 전달한다(캐릭터 이름·제목 제외).
    assert classifier.calls == [ClassificationInput("마법 학교", ("원래 상세",))]
    assert state(db, story) == {"pending": False, "categories": ["modern", "romance"]}


def test_draft_temporary_replay_and_resave_do_not_dispatch_CREATE_US_09_AC04(api, db, db_url):
    classifier = FakeClassifier()
    client = api(classifier)
    story = create_complete(client, db_url)
    assert put(client, story, body("draft"), "draft").status_code == 200
    temp = {**body("private"), "kind": "temporary"}
    assert put(client, story, temp, "stage").status_code == 200
    assert classifier.calls == []
    assert state(db, story) == {"pending": False, "categories": []}
    assert put(client, story, body("private"), "register").status_code == 200
    assert len(classifier.calls) == 1
    # 같은 요청키 재전송은 기록한 응답만 돌려주고 다시 분류하지 않는다.
    db.execute("DELETE FROM story_categories WHERE story_id=%s", (story["id"],))
    db.execute("UPDATE stories SET category_classification_needed=true WHERE id=%s", (story["id"],))
    assert put(client, story, body("private"), "register").status_code == 200
    # 이미 등록된 스토리의 재저장·공개 전환은 최초 등록이 아니다.
    assert put(client, story, body("private"), "resave").status_code == 200
    assert put(client, story, body("public"), "publish").status_code == 200
    assert len(classifier.calls) == 1
    assert state(db, story) == {"pending": True, "categories": []}


def test_first_worker_runs_after_connection_release_and_response_CREATE_US_09_AC04(db, db_url):
    events = []
    app = create_app(db_url, category_classifier=lambda _: events.append("classify") or ["modern"])

    def tracked_conn():
        yield from get_conn()
        events.append("release")

    async def observe(scope, receive, send):
        async def capture(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                events.append("sent")

        await app(scope, receive, capture)

    app.dependency_overrides[get_conn] = tracked_conn
    with TestClient(observe) as client:
        client.headers["X-User-Id"] = "100"
        story = create_complete(client, db_url)
        events.clear()
        assert put(client, story, body("private"), "register").status_code == 200
    assert events == ["release", "sent", "classify"]
    assert state(db, story) == {"pending": False, "categories": ["modern"]}


def test_no_classifier_by_default_keeps_pending_CREATE_US_09_AC04(api, db, db_url):
    client = api()
    story = create_complete(client, db_url)
    assert put(client, story, body("public"), "register").status_code == 200
    assert state(db, story) == {"pending": True, "categories": []}


@pytest.mark.parametrize(
    "result",
    [("unknown",), ("romance", "romance"), (), ("bl", "gl", "modern", "romance"), "romance"],
)
def test_invalid_result_keeps_story_pending_CREATE_US_09_AC03(api, db, db_url, result):
    classifier = FakeClassifier(result)
    client = api(classifier)
    story = create_complete(client, db_url)
    assert put(client, story, body("private"), "register").status_code == 200
    assert len(classifier.calls) == 1
    assert state(db, story) == {"pending": True, "categories": []}


def test_classifier_failure_does_not_affect_response_CREATE_US_09_AC04(api, db, db_url):
    classifier = FakeClassifier(RuntimeError("upstream said: secret prompt"))
    client = api(classifier)
    story = create_complete(client, db_url)
    response = put(client, story, body("private"), "register")
    assert response.status_code == 200
    assert response.json()["save_state"] == "private"
    assert state(db, story) == {"pending": True, "categories": []}


def test_change_during_inference_is_stale_and_stays_pending_CREATE_US_09_AC04(api, db, db_url):
    def edit(_):
        # 추론은 트랜잭션 밖이므로 다른 연결의 확정 변경이 잠금 대기 없이 끝난다.
        with psycopg.connect(db_url, options="-c lock_timeout=2000") as editing:
            editing.execute(
                "UPDATE stories SET world_description='바뀐 세계관' WHERE id=%s", (story["id"],)
            )

    classifier = FakeClassifier(effect=edit)
    client = api(classifier)
    story = create_complete(client, db_url)
    assert put(client, story, body("private"), "register").status_code == 200
    assert len(classifier.calls) == 1
    assert state(db, story) == {"pending": True, "categories": []}


def test_first_worker_skips_ineligible_stories_CREATE_US_09_AC04(db, db_url):
    def unexpected(_):
        raise AssertionError("ineligible story reached classifier")

    def seed(state, pending, categories=()):
        story_id = db.execute(
            """INSERT INTO stories(owner_id,save_state,world_description,
                                   category_classification_needed)
               VALUES (100,%s,'세계관',%s) RETURNING id""",
            (state, pending),
        ).fetchone()["id"]
        for code in categories:
            db.execute("INSERT INTO story_categories VALUES (%s,%s)", (story_id, code))
        return story_id

    prior = seed("private", True, ["modern"])
    # 최초 분류 작업 기록(AC02)이 없는 스토리는 등록 대기 표시가 있어도 다루지 않는다.
    unqueued = seed("public", True)
    for story_id in (seed("draft", False), seed("private", False), prior, unqueued, 999_999):
        assert classify_first(db_url, story_id, unexpected) == "ineligible"
    assert state(db, {"id": prior}) == {"pending": True, "categories": ["modern"]}
    eligible = seed("public", True)
    db.execute("INSERT INTO story_first_classification_jobs(story_id) VALUES (%s)", (eligible,))
    assert classify_first(db_url, eligible, lambda _: ["gl"]) == "completed"
    assert state(db, {"id": eligible}) == {"pending": False, "categories": ["gl"]}
    assert classify_first(db_url, eligible, unexpected) == "ineligible"
