"""CREATE-US-09-AC02/AC07: 독립 실행기의 시작·주기·종료 경계."""

from threading import Event

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import tuple_row

from app.features.creation.category_classification import runtime
from app.features.creation.category_classification.worker import RetryableClassificationError
from app.main import create_app
from tests.features.creation.category_classification.test_first_classification import (
    create_complete,
    state,
)
from tests.features.creation.speech_style.test_common_save_api import body, put


class Clock:
    def __init__(self, end):
        self.now = 0.0
        self.end = end

    def is_set(self):
        return self.now >= self.end

    def wait(self, timeout: float) -> bool:
        assert timeout > 0
        self.now += timeout
        return self.is_set()


def test_worker_recovers_pending_and_runs_hourly_only_at_deadline_CREATE_US_09_AC02_AC07(
    db, db_url, dev_user_header_enabled, monkeypatch
):
    clock = Clock(3601)
    monkeypatch.setattr(runtime, "monotonic", lambda: clock.now)
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"
        first = create_complete(client, db_url)
        second = create_complete(client, db_url)
        assert put(client, first, body("private"), "first").status_code == 200
        assert put(client, second, body("private"), "second").status_code == 200
        db.execute(
            "UPDATE stories SET world_description='시간 배치 변경본' WHERE id=%s", (second["id"],)
        )
        db.execute(
            "INSERT INTO story_categories (story_id,category_code) VALUES (%s,'romance')",
            (second["id"],),
        )
        db.execute(
            "UPDATE story_first_classification_jobs SET status='completed' WHERE story_id=%s",
            (second["id"],),
        )
    db.execute(
        "UPDATE story_first_classification_jobs SET status='running', attempts=1 WHERE story_id=%s",
        (first["id"],),
    )
    calls = []

    def classify(value):
        calls.append((clock.now, value.world_description))
        if len(calls) == 1:
            assert state(db, second) == {"pending": True, "categories": ["romance"]}
        return ("modern",)

    assert runtime.run_worker(db_url, classify, clock) is True
    assert calls == [(0, "마법 학교"), (3600, "시간 배치 변경본")]
    assert state(db, first) == {"pending": False, "categories": ["modern"]}
    assert state(db, second) == {"pending": False, "categories": ["modern"]}
    assert db.execute(
        "SELECT status, attempts FROM story_first_classification_jobs WHERE story_id=%s",
        (first["id"],),
    ).fetchone() == {"status": "completed", "attempts": 2}


def test_duplicate_worker_exits_without_running_and_lock_is_released_CREATE_US_09_AC07(db_url):
    stop = Event()
    with psycopg.connect(db_url, autocommit=True) as incumbent:
        assert incumbent.execute(
            "SELECT pg_try_advisory_lock(%s)", (runtime.SCHEDULER_LOCK,)
        ).fetchone() == (True,)
        assert runtime.run_worker(db_url, lambda _: (), stop) is False
    stop.set()
    assert runtime.run_worker(db_url, lambda _: (), stop) is True
    with psycopg.connect(db_url, autocommit=True) as successor:
        assert successor.execute(
            "SELECT pg_try_advisory_lock(%s)", (runtime.SCHEDULER_LOCK,)
        ).fetchone() == (True,)


def test_shutdown_preserves_retryable_job_for_restart_CREATE_US_09_AC02(
    db, db_url, dev_user_header_enabled
):
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"
        story = create_complete(client, db_url)
        assert put(client, story, body("private"), "register").status_code == 200
    stop = Event()
    calls = []

    def temporary_failure(value):
        calls.append(value)
        stop.set()
        raise RetryableClassificationError

    assert runtime.run_worker(db_url, temporary_failure, stop) is True
    assert len(calls) == 1
    assert db.execute(
        "SELECT status, attempts, failure_reason "
        "FROM story_first_classification_jobs WHERE story_id=%s",
        (story["id"],),
    ).fetchone() == {"status": "pending", "attempts": 1, "failure_reason": "temporary"}


class FakeResult:
    def fetchone(self):
        return (True,)


class FakeConnection:
    def __init__(self, calls=None):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args):
        return FakeResult()


def _fake_scheduler(monkeypatch, clock, events, recover=None):
    """리더 연결·복구·시간 배치를 가짜로 바꿔 실행 순서만 기록한다(DB 없음)."""
    monkeypatch.setattr(runtime, "monotonic", lambda: clock.now)
    monkeypatch.setattr(runtime.psycopg, "connect", lambda *_a, **_k: FakeConnection())
    monkeypatch.setattr(runtime, "_require_jobs_table", lambda _conn: None)

    def fake_recover(_url, _classify, _should_stop, yield_at):
        events.append(("recover", clock.now))
        if recover is not None:
            recover(yield_at)

    monkeypatch.setattr(runtime, "_recover", fake_recover)
    monkeypatch.setattr(runtime, "_hourly", lambda *_a: events.append(("hourly", clock.now)))


def test_due_hourly_runs_before_recovery_collection_CREATE_US_09_AC07(monkeypatch):
    clock = Clock(3601)
    events = []
    _fake_scheduler(monkeypatch, clock, events)
    assert runtime.run_worker("postgresql://fake", lambda _: (), clock) is True
    # 3,600초에 둘 다 마감이면 시간 배치를 먼저 보낸다.
    assert events[-2:] == [("hourly", 3600), ("recover", 3600)]
    assert events.count(("hourly", 3600)) == 1


def test_recovery_collection_defers_when_hourly_is_within_bound_CREATE_US_09_AC07(monkeypatch):
    clock = Clock(3601)
    events = []
    monkeypatch.setattr(runtime, "RECOVERY_INTERVAL_SECONDS", 7)
    _fake_scheduler(monkeypatch, clock, events)
    assert runtime.run_worker("postgresql://fake", lambda _: (), clock) is True
    # 3,591초는 상한(4초)보다 여유가 있어 수집, 3,598초는 2초 남아 배치 직후로 미룬다.
    assert 3600 - 3598 < runtime.RECOVERY_COLLECT_BOUND_SECONDS <= 3600 - 3591
    assert events[-3:] == [("recover", 3591), ("hourly", 3600), ("recover", 3600)]


def test_hourly_due_after_slow_recovery_runs_before_next_collection_CREATE_US_09_AC07(
    monkeypatch,
):
    clock = Clock(3611)
    events = []

    def slow_first_job(_yield_at):
        if len(events) == 1:
            clock.now = 3610

    _fake_scheduler(monkeypatch, clock, events, recover=slow_first_job)
    assert runtime.run_worker("postgresql://fake", lambda _: (), clock) is True
    assert events == [("recover", 0), ("hourly", 3610)]


def test_recovery_bounds_collection_and_yields_inside_job_CREATE_US_09_AC02_AC07(monkeypatch):
    clock = Clock(10_000)
    monkeypatch.setattr(runtime, "monotonic", lambda: clock.now)
    connects = []

    def fake_connect(_url, **kwargs):
        connects.append(kwargs)
        return FakeConnection()

    monkeypatch.setattr(runtime.psycopg, "connect", fake_connect)
    monkeypatch.setattr(runtime.repository, "recoverable_first_ids", lambda _conn: [1, 2])
    seen = []

    def fake_classify_first(_url, story_id, _classify, *, should_stop):
        before = should_stop()
        clock.now = 100  # 작업 도중 시간 배치 마감이 지났다.
        seen.append((story_id, before, should_stop()))
        return "stopped"

    monkeypatch.setattr(runtime.worker, "classify_first", fake_classify_first)
    runtime._recover("postgresql://fake", lambda _: (), lambda: False, yield_at=100)
    assert connects == [runtime.RECOVERY_CONNECT_KWARGS]
    assert connects[0]["connect_timeout"] == runtime.RECOVERY_CONNECT_TIMEOUT_SECONDS
    assert "statement_timeout=" in connects[0]["options"]
    # 작업 안의 시도 경계도 시간 배치 마감에서 양보하고, 다음 작업은 시작하지 않는다.
    assert seen == [(1, False, True)]

    # 리더 확인 중에 시간 배치 마감이 지나거나 종료 요청이 오면 작업을 보내지 않는다.
    flag = runtime.StopFlag()
    for probe in (lambda _leader: setattr(clock, "now", 100), lambda _leader: flag.request()):
        clock.now = 0
        seen.clear()
        monkeypatch.setattr(runtime, "_check_leader", probe)
        runtime._recover(
            "postgresql://fake", lambda _: (), runtime._stop_check(flag, None), yield_at=100
        )
        assert seen == []


def test_stop_requested_during_leader_probe_prevents_dispatch_CREATE_US_09_AC07(monkeypatch):
    clock = Clock(10_000)
    events = []
    _fake_scheduler(monkeypatch, clock, events)
    flag = runtime.StopFlag()
    monkeypatch.setattr(runtime, "_check_leader", lambda _leader: flag.request())
    assert runtime.run_worker("postgresql://fake", lambda _: (), flag) is True
    assert events == []


def test_leader_lost_after_reservation_restores_attempt_CREATE_US_09_AC02(
    db, db_url, dev_user_header_enabled, monkeypatch
):
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"
        story = create_complete(client, db_url)
        assert put(client, story, body("private"), "register").status_code == 200
    job_sql = (
        "SELECT status, attempts, failure_reason "
        "FROM story_first_classification_jobs WHERE story_id=%s"
    )
    db.execute(
        "UPDATE story_first_classification_jobs "
        "SET status='pending', attempts=2, failure_reason='temporary' WHERE story_id=%s",
        (story["id"],),
    )
    calls = []

    def classify(value):
        calls.append(value)
        return ("modern",)

    # 시도 기록 직후(같은 트랜잭션, 커밋 전) 확인에서만 리더 연결 끊김을 올린다.
    reserved = {"conn": None, "fired": False}
    original_start = runtime.repository.start_first_attempt

    def capture_start(conn, story_id):
        original_start(conn, story_id)
        reserved["conn"] = conn

    monkeypatch.setattr(runtime.repository, "start_first_attempt", capture_start)

    def lost_before_commit():
        conn = reserved["conn"]
        if conn is not None and not reserved["fired"]:
            reserved["fired"] = True
            assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
            raise runtime.LeaderConnectionLost
        return False

    with pytest.raises(runtime.LeaderConnectionLost):
        runtime._recover(db_url, classify, lost_before_commit, yield_at=float("inf"))
    assert reserved["fired"] is True
    assert calls == []
    assert db.execute(job_sql, (story["id"],)).fetchone() == {
        "status": "pending",
        "attempts": 2,
        "failure_reason": "temporary",
    }
    assert state(db, story)["pending"] is True

    # 재시작하면 남은 마지막 시도로 완료한다.
    monkeypatch.setattr(runtime.repository, "start_first_attempt", original_start)
    runtime._recover(db_url, classify, lambda: False, yield_at=float("inf"))
    assert len(calls) == 1
    assert db.execute(job_sql, (story["id"],)).fetchone() == {
        "status": "completed",
        "attempts": 3,
        "failure_reason": None,
    }
    assert state(db, story) == {"pending": False, "categories": ["modern"]}


def _terminate_backends(db_url, *pids):
    """별도 관리 연결로 세션을 강제 종료하고, 실제로 사라질 때까지 기다린다."""
    with psycopg.connect(db_url, autocommit=True, row_factory=tuple_row) as admin:
        for pid in pids:
            assert admin.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone() == (True,)
        for _ in range(100):
            alive = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = ANY(%s)", (list(pids),)
            ).fetchone()[0]
            if alive == 0:
                return
            admin.execute("SELECT pg_sleep(0.05)")
        raise AssertionError("terminated backends are still alive")


def test_leader_and_worker_sessions_lost_after_reservation_restores_attempt_CREATE_US_09_AC02(
    db, db_url, dev_user_header_enabled, monkeypatch
):
    """예약 커밋 뒤·공급자 호출 전에 리더와 작업 세션이 함께 끊겨도 시도를 쓰지 않는다."""
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"
        story = create_complete(client, db_url)
        assert put(client, story, body("private"), "register").status_code == 200
    job_sql = (
        "SELECT status, attempts, failure_reason "
        "FROM story_first_classification_jobs WHERE story_id=%s"
    )
    db.execute(
        "UPDATE story_first_classification_jobs "
        "SET status='pending', attempts=2, failure_reason='temporary' WHERE story_id=%s",
        (story["id"],),
    )
    calls = []

    def classify(value):
        calls.append(value)
        return ("modern",)

    # 시도 기록을 쓰는 작업 연결을 잡아 두고, 바로 다음 확인에서만 끊김을 일으킨다.
    reserved = {"pid": None, "fired": False}
    original_start = runtime.repository.start_first_attempt

    def capture_start(conn, story_id):
        original_start(conn, story_id)
        if reserved["pid"] is None:
            reserved["pid"] = conn.info.backend_pid

    monkeypatch.setattr(runtime.repository, "start_first_attempt", capture_start)

    with psycopg.connect(db_url, **runtime.LEADER_CONNECT_KWARGS) as leader:
        leader_pid = leader.info.backend_pid
        leader_check = runtime._stop_check(runtime.StopFlag(), leader)

        def both_sessions_lost_before_commit():
            if reserved["pid"] is not None and not reserved["fired"]:
                reserved["fired"] = True
                # 작업 세션과 리더 세션을 모두 끊은 뒤 실제 리더 확인으로 끊김을 올린다.
                _terminate_backends(db_url, reserved["pid"], leader_pid)
            return leader_check()

        with pytest.raises(runtime.LeaderConnectionLost):
            runtime._recover(
                db_url, classify, both_sessions_lost_before_commit, yield_at=float("inf")
            )
    assert reserved["fired"] is True
    assert calls == []
    assert db.execute(job_sql, (story["id"],)).fetchone() == {
        "status": "pending",
        "attempts": 2,
        "failure_reason": "temporary",
    }
    assert state(db, story)["pending"] is True

    # 재시작하면 남은 마지막 시도로 완료한다.
    monkeypatch.setattr(runtime.repository, "start_first_attempt", original_start)
    runtime._recover(db_url, classify, lambda: False, yield_at=float("inf"))
    assert len(calls) == 1
    assert db.execute(job_sql, (story["id"],)).fetchone() == {
        "status": "completed",
        "attempts": 3,
        "failure_reason": None,
    }
    assert state(db, story) == {"pending": False, "categories": ["modern"]}


def test_worker_rejects_unmigrated_target_CREATE_US_09_AC02(db_url):
    with psycopg.connect(db_url, row_factory=tuple_row) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("DROP TABLE story_first_classification_jobs")
            with pytest.raises(runtime.MissingJobsTable):
                runtime._require_jobs_table(conn)
