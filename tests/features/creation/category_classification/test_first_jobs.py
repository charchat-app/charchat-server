"""CREATE-US-09-AC02: 최초 분류 작업 기록·상한 있는 재시도·복구. 격리 PostgreSQL만 쓴다."""

import logging
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.features.creation.category_classification import repository, service, worker
from app.features.creation.category_classification.worker import (
    MAX_FIRST_ATTEMPTS,
    ClassificationInput,
    RetryableClassificationError,
    classify_first,
    recover_first_jobs,
)
from app.main import create_app
from tests.features.creation.speech_style.test_common_save_api import body, complete, put

STORIES = "/api/v1/creation/stories"
pytestmark = pytest.mark.usefixtures("dev_user_header_enabled")
PRIVATE = "비밀 세계관 원문"


class Crash(BaseException):
    """추론 도중 프로세스가 멈춘 상황. 일반 예외 처리에 잡히지 않는다."""


class Script:
    """호출마다 정해 둔 결과를 돌려주거나 예외를 던지는 가짜 분류기."""

    def __init__(self, *steps, effect=None):
        self.steps = list(steps)
        self.effect = effect
        self.calls = []

    def __call__(self, value):
        self.calls.append(value)
        if self.effect is not None:
            self.effect(value)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        return step


def unexpected(_):
    raise AssertionError("classifier must not be called")


def register(db_url, target="private", classifier=None):
    """앱을 띄워 최초 등록하고 앱을 닫는다(디스패처 정지와 같다)."""
    with TestClient(create_app(db_url, category_classifier=classifier)) as client:
        client.headers["X-User-Id"] = "100"
        response = client.post(STORIES, json={"title": "검수 스토리", "world_description": PRIVATE})
        assert response.status_code == 201
        story = response.json()
        complete(db_url, story)
        assert put(client, story, body(target), "register").status_code == 200
    return story["id"]


def job(db, story_id):
    return db.execute(
        """SELECT status, attempts, failure_reason
           FROM story_first_classification_jobs WHERE story_id=%s""",
        (story_id,),
    ).fetchone()


def result(db, story_id):
    return db.execute(
        """SELECT category_classification_needed AS pending,
                  ARRAY(SELECT category_code FROM story_categories WHERE story_id=s.id
                        ORDER BY category_code) AS categories
           FROM stories s WHERE id=%s""",
        (story_id,),
    ).fetchone()


def seed(db, *, story_id=None, status="pending", attempts=0):
    """확정 등록되어 최초 분류를 기다리는 스토리와 작업 기록을 직접 만든다."""
    columns = "id," if story_id is not None else ""
    override = "OVERRIDING SYSTEM VALUE" if story_id is not None else ""
    values = [story_id] if story_id is not None else []
    story_id = db.execute(
        f"""INSERT INTO stories({columns}owner_id,save_state,world_description,
                                category_classification_needed)
            {override} VALUES ({"%s," if values else ""}100,'private',%s,true) RETURNING id""",
        (*values, PRIVATE),
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO story_first_classification_jobs(story_id,status,attempts)
           VALUES (%s,%s,%s)""",
        (story_id, status, attempts),
    )
    return story_id


PENDING_JOB = {"status": "pending", "attempts": 0, "failure_reason": None}
WAITING = {"pending": True, "categories": []}


@pytest.mark.parametrize("target", ["private", "public"])
def test_first_registration_enqueues_one_pending_job_CREATE_US_09_AC02(db, db_url, target):
    story_id = register(db_url, target)
    assert job(db, story_id) == PENDING_JOB
    assert result(db, story_id) == WAITING


def test_draft_and_resave_do_not_enqueue_CREATE_US_09_AC02(db, db_url):
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"
        story = client.post(STORIES, json={"title": "t", "world_description": PRIVATE}).json()
        complete(db_url, story)
        assert put(client, story, body("draft"), "draft").status_code == 200
        assert job(db, story["id"]) is None
        assert put(client, story, body("private"), "register").status_code == 200
        assert put(client, story, body("private"), "register").status_code == 200  # 재전송
        assert put(client, story, body("public"), "publish").status_code == 200
    count = db.execute("SELECT count(*) AS n FROM story_first_classification_jobs").fetchone()
    assert count["n"] == 1


def test_enqueue_rolls_back_with_registration_flag_CREATE_US_09_AC02(db):
    story_id = db.execute(
        "INSERT INTO stories(owner_id,save_state) VALUES (100,'private') RETURNING id"
    ).fetchone()["id"]
    with pytest.raises(RuntimeError), db.transaction():
        assert service.mark_after_registration(
            db, story_id, previous_state="draft", current_state="private"
        )
        assert job(db, story_id) == PENDING_JOB
        raise RuntimeError("registration failed after marking")
    assert job(db, story_id) is None
    assert result(db, story_id) == {"pending": False, "categories": []}


def test_restart_without_classifier_then_recovery_CREATE_US_09_AC02(db, db_url):
    # 분류기 없이 등록한 뒤 앱을 닫는다. 작업은 DB에 남는다.
    story_id = register(db_url, classifier=None)
    assert job(db, story_id) == PENDING_JOB
    classify = Script(("romance", "modern"))
    assert recover_first_jobs(db_url, classify) == {"completed": 1}
    assert classify.calls == [ClassificationInput(PRIVATE, ("원래 상세",))]
    assert job(db, story_id) == {"status": "completed", "attempts": 1, "failure_reason": None}
    assert result(db, story_id) == {"pending": False, "categories": ["modern", "romance"]}
    # 완료된 작업은 다시 고르지 않는다.
    assert recover_first_jobs(db_url, unexpected) == {}


def test_crashed_attempt_is_counted_and_reclaimed_by_recovery_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url)
    with pytest.raises(Crash):
        classify_first(db_url, story_id, Script(Crash()))
    # 공급자 호출 전에 시도를 세므로 멈춘 시도도 남는다. 연결이 닫혀 세션 잠금도 풀린다.
    assert job(db, story_id) == {"status": "running", "attempts": 1, "failure_reason": None}
    classify = Script(["gl"])
    assert recover_first_jobs(db_url, classify) == {"completed": 1}
    assert len(classify.calls) == 1
    assert job(db, story_id) == {"status": "completed", "attempts": 2, "failure_reason": None}
    assert result(db, story_id) == {"pending": False, "categories": ["gl"]}


def test_temporary_then_success_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url)
    classify = Script(RetryableClassificationError("429"), ["modern"])
    assert classify_first(db_url, story_id, classify) == "completed"
    assert len(classify.calls) == 2
    assert job(db, story_id) == {"status": "completed", "attempts": 2, "failure_reason": None}
    assert result(db, story_id) == {"pending": False, "categories": ["modern"]}


def test_temporary_failures_stop_at_cap_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url)
    classify = Script(RetryableClassificationError("503"))
    assert classify_first(db_url, story_id, classify) == "failed"
    assert MAX_FIRST_ATTEMPTS == 3
    assert len(classify.calls) == 3  # 최초 1회 + 재시도 2회
    failed = {"status": "failed", "attempts": 3, "failure_reason": "temporary"}
    assert job(db, story_id) == failed
    assert result(db, story_id) == WAITING
    # 실패로 끝난 작업은 복구·재호출로 다시 실행하지 않는다.
    assert recover_first_jobs(db_url, unexpected) == {}
    assert classify_first(db_url, story_id, unexpected) == "ineligible"
    assert job(db, story_id) == failed


@pytest.mark.parametrize("status", ["running", "pending"])
def test_exhausted_job_is_failed_without_provider_call_CREATE_US_09_AC02(db, db_url, status):
    story_id = seed(db, status=status, attempts=MAX_FIRST_ATTEMPTS)
    assert recover_first_jobs(db_url, unexpected) == {"failed": 1}
    reason = "interrupted" if status == "running" else None
    assert job(db, story_id) == {"status": "failed", "attempts": 3, "failure_reason": reason}
    assert result(db, story_id) == WAITING


@pytest.mark.parametrize(
    "outcome",
    [ValueError(f"upstream echoed {PRIVATE}"), ["unknown"], [], ["bl", "bl"], "romance"],
)
def test_permanent_failure_is_not_retried_CREATE_US_09_AC02(db, db_url, caplog, outcome):
    story_id = register(db_url)
    caplog.set_level(logging.DEBUG)
    classify = Script(outcome)
    assert classify_first(db_url, story_id, classify) == "failed"
    assert len(classify.calls) == 1
    assert job(db, story_id) == {"status": "failed", "attempts": 1, "failure_reason": "permanent"}
    assert result(db, story_id) == WAITING
    assert recover_first_jobs(db_url, unexpected) == {}
    # 공급자 예외 문구·입력 원문을 로그에 남기지 않는다.
    assert PRIVATE not in caplog.text and "upstream" not in caplog.text


def test_already_completed_job_is_not_classified_again_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url)
    assert classify_first(db_url, story_id, Script(["romance"])) == "completed"
    assert classify_first(db_url, story_id, unexpected) == "ineligible"
    assert recover_first_jobs(db_url, unexpected) == {}
    assert job(db, story_id) == {"status": "completed", "attempts": 1, "failure_reason": None}


def test_relevant_committed_edit_reopens_failed_first_job_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url, classifier=Script(["unknown"]))
    assert job(db, story_id)["status"] == "failed"
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"
        endpoint = f"{STORIES}/{story_id}"
        edit = {"title": "제목만 변경", "subtitle": "한 줄 소개", "world_description": PRIVATE}
        assert client.put(endpoint, json=edit).status_code == 200
        assert job(db, story_id)["status"] == "failed"
        assert (
            client.put(endpoint, json={**edit, "world_description": "최종 세계관"}).status_code
            == 200
        )
    assert job(db, story_id) == PENDING_JOB
    classify = Script(["modern"])
    assert recover_first_jobs(db_url, classify) == {"completed": 1}
    assert classify.calls == [ClassificationInput("최종 세계관", ("원래 상세",))]


def test_overlapping_workers_skip_story_held_by_another_session_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url)
    other = seed(db)

    def nested(_):
        # 같은 스토리를 맡은 작업이 돌고 있으면 다른 실행은 건너뛰고 기록을 바꾸지 않는다.
        assert classify_first(db_url, story_id, unexpected) == "busy"
        assert job(db, story_id) == {"status": "running", "attempts": 1, "failure_reason": None}
        # 다른 스토리와 시간 배치 잠금은 서로 막지 않는다.
        assert classify_first(db_url, other, Script(["gl"])) == "completed"
        assert worker.run_cycle(db_url, unexpected).busy is False

    assert classify_first(db_url, story_id, Script(["modern"], effect=nested)) == "completed"
    assert job(db, story_id)["status"] == "completed"


def test_orphan_running_is_reclaimed_only_after_lock_CREATE_US_09_AC02(db, db_url):
    story_id = seed(db, status="running", attempts=1)
    with psycopg.connect(db_url, autocommit=True) as holder:
        assert repository.try_lock_first(holder, story_id)
        assert recover_first_jobs(db_url, unexpected) == {"busy": 1}
        assert job(db, story_id) == {"status": "running", "attempts": 1, "failure_reason": None}
    # 잠금을 가진 세션이 끝난 뒤에야 남은 실행 중 기록을 회수한다.
    assert recover_first_jobs(db_url, Script(["modern"])) == {"completed": 1}
    assert job(db, story_id) == {"status": "completed", "attempts": 2, "failure_reason": None}


def test_change_during_inference_stays_recoverable_for_latest_input_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url)

    def edit(_):
        # 추론 동안 스토리 행 잠금을 잡지 않으므로 확정 변경이 대기 없이 끝난다.
        with psycopg.connect(db_url, options="-c lock_timeout=2000") as editing:
            editing.execute(
                "UPDATE stories SET world_description='바뀐 세계관' WHERE id=%s", (story_id,)
            )

    classify = Script(["modern"], effect=edit)
    assert classify_first(db_url, story_id, classify) == "stale"
    # 한 번의 실행 안에서 다시 돌지 않고, 성공을 기록하지 않은 채 대기로 돌린다.
    assert len(classify.calls) == 1
    assert job(db, story_id) == {"status": "pending", "attempts": 1, "failure_reason": "stale"}
    assert result(db, story_id) == WAITING
    latest = Script(["romance"])
    assert recover_first_jobs(db_url, latest) == {"completed": 1}
    assert latest.calls == [ClassificationInput("바뀐 세계관", ("원래 상세",))]
    assert job(db, story_id) == {"status": "completed", "attempts": 2, "failure_reason": None}
    assert result(db, story_id) == {"pending": False, "categories": ["romance"]}


def test_deleted_story_job_fails_without_provider_call_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url)
    db.execute("UPDATE stories SET deleted_at=clock_timestamp() WHERE id=%s", (story_id,))
    assert recover_first_jobs(db_url, unexpected) == {"ineligible": 1}
    assert job(db, story_id) == {"status": "failed", "attempts": 0, "failure_reason": "ineligible"}


def test_edit_on_final_attempt_still_queues_new_input_CREATE_US_09_AC02(db, db_url):
    story_id = seed(db, status="running", attempts=2)

    def edit(_):
        db.execute("UPDATE stories SET world_description='새 입력' WHERE id=%s", (story_id,))

    assert classify_first(db_url, story_id, Script(["modern"], effect=edit)) == "stale"
    assert job(db, story_id) == {"status": "pending", "attempts": 0, "failure_reason": "stale"}
    classify = Script(["romance"])
    assert recover_first_jobs(db_url, classify) == {"completed": 1}
    assert classify.calls == [ClassificationInput("새 입력", ())]


def test_crash_on_final_attempt_does_not_lose_committed_edit_CREATE_US_09_AC02(db, db_url):
    story_id = register(db_url)
    db.execute(
        "UPDATE story_first_classification_jobs SET attempts=2 WHERE story_id=%s", (story_id,)
    )
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"

        def edit(_):
            payload = {
                "title": "검수 스토리",
                "subtitle": "한 줄 소개",
                "world_description": "새 입력",
            }
            assert client.put(f"{STORIES}/{story_id}", json=payload).status_code == 200

        with pytest.raises(Crash):
            classify_first(db_url, story_id, Script(Crash(), effect=edit))
    assert job(db, story_id) == PENDING_JOB
    classify = Script(["modern"])
    assert recover_first_jobs(db_url, classify) == {"completed": 1}
    assert classify.calls == [ClassificationInput("새 입력", ("원래 상세",))]


def test_job_migration_creates_table_and_preserves_work_CREATE_US_09_AC02(db):
    migration = Path(__file__).resolve().parents[4] / (
        "app/db/migrations/create_us09_first_classification_jobs.sql"
    )
    sql = migration.read_text(encoding="utf-8")
    db.execute("DROP TABLE story_first_classification_jobs")
    db.execute(sql)
    story_id = seed(db, attempts=2)
    db.execute(sql)
    assert job(db, story_id) == {"status": "pending", "attempts": 2, "failure_reason": None}


def test_bigint_story_id_lock_key_CREATE_US_09_AC02(db, db_url):
    story_id = seed(db, story_id=5_000_000_123)
    assert classify_first(db_url, story_id, Script(["modern"])) == "completed"
    assert job(db, story_id)["status"] == "completed"


def test_dispatch_after_registration_uses_job_CREATE_US_09_AC02(db, db_url):
    classify = Script(["modern"])
    story_id = register(db_url, classifier=classify)
    assert len(classify.calls) == 1
    assert job(db, story_id) == {"status": "completed", "attempts": 1, "failure_reason": None}
