"""CREATE-US-09 등록 후 확정 변경을 모아 처리하는 독립 분류 작업."""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic
from typing import Literal

import psycopg

from app.features.creation.category_classification import repository

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 3600
# 이 DB의 카테고리 배치만 직렬화한다. 스토리 행 잠금과는 별개다.
CYCLE_LOCK = 0x43415409
MAX_FIRST_ATTEMPTS = 3


class RetryableClassificationError(Exception):
    """다시 시도할 수 있는 분류기 오류."""


@dataclass(frozen=True)
class ClassificationInput:
    world_description: str | None
    character_descriptions: tuple[str | None, ...]


Classifier = Callable[[ClassificationInput], list[str] | tuple[str, ...]]


@dataclass
class CycleReport:
    selected: int = 0
    completed: int = 0
    stale: int = 0
    failed: int = 0
    busy: bool = False


def _validate(raw: object, known_codes: set[str]) -> tuple[str, ...] | None:
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= 3:
        return None
    if any(not isinstance(code, str) or code not in known_codes for code in raw):
        return None
    if len(set(raw)) != len(raw):
        return None
    return tuple(raw)


def _infer(
    classify: Classifier, snapshot, known_codes: set[str], *, propagate_retryable=False
) -> tuple[str, ...] | None:
    """트랜잭션·행 잠금 밖에서 호출한다. 실패·잘못된 출력은 None."""
    world, characters = snapshot
    value = ClassificationInput(world, tuple(description for _, description in characters))
    try:
        return _validate(classify(value), known_codes)
    except RetryableClassificationError:
        if propagate_retryable:
            raise
        return None
    except Exception:
        # 공급자 예외에 입력/응답이 포함될 수 있으므로 본문을 기록하지 않는다.
        return None


def _save_if_current(conn, story_id, snapshot, codes, *, first_only=False) -> bool:
    """스토리 행을 잠근 뒤 입력이 추론 때와 같을 때만 결과를 통째로 바꾼다. 아니면 False."""
    with conn.transaction():
        if not repository.lock_pending(conn, story_id, first_only=first_only):
            return False
        # 모든 확정 기본 정보/캐릭터 쓰기가 같은 스토리 행을 먼저 잠근다.
        if repository.read_inputs(conn, story_id, pending_only=True) != snapshot:
            return False
        repository.save_result(conn, story_id, codes)
    return True


def run_cycle(database_url: str, classify: Classifier) -> CycleReport:
    """한 주기의 대상을 한 번만 수집하고, 추론 동안 스토리 행 잠금을 잡지 않는다."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (CYCLE_LOCK,)).fetchone()[0]:
            return CycleReport(busy=True)
        # 연결 종료 시 세션 잠금도 풀린다. 추론은 트랜잭션 밖에서 실행한다.
        story_ids = repository.pending_ids(conn)
        known_codes = repository.category_codes(conn)
        report = CycleReport(selected=len(story_ids))
        for story_id in story_ids:
            snapshot = repository.read_inputs(conn, story_id, pending_only=True)
            if snapshot is None:
                report.stale += 1
                continue
            codes = _infer(classify, snapshot, known_codes)
            if codes is None:
                report.failed += 1
                continue
            if _save_if_current(conn, story_id, snapshot, codes):
                report.completed += 1
            else:
                report.stale += 1
        return report


FirstOutcome = Literal["completed", "stale", "failed", "ineligible", "error", "busy"]


def _first_attempts(conn, story_id, classify) -> FirstOutcome:
    known_codes = repository.category_codes(conn)
    while True:
        with conn.transaction():
            eligible = repository.lock_pending(conn, story_id, first_only=True)
            job = repository.first_job(conn, story_id)
            if job is None or job[0] not in ("pending", "running"):
                return "ineligible"
            if not eligible:
                repository.finish_first_attempt(conn, story_id, "failed", "ineligible")
                return "ineligible"
            status, attempts, last_reason = job
            if attempts == MAX_FIRST_ATTEMPTS:
                reason = "interrupted" if status == "running" else last_reason
                repository.finish_first_attempt(conn, story_id, "failed", reason)
                return "failed"
            snapshot = repository.read_inputs(conn, story_id, first_only=True)
            repository.start_first_attempt(conn, story_id)
        attempt = attempts + 1
        try:
            codes = _infer(classify, snapshot, known_codes, propagate_retryable=True)
            reason = "permanent"
        except RetryableClassificationError:
            codes, reason = None, "temporary"

        # 확정 저장과 동일한 순서로 스토리 행을 잠근 뒤 작업 기록을 갱신한다.
        with conn.transaction():
            if not repository.lock_pending(conn, story_id, first_only=True):
                repository.finish_first_attempt(conn, story_id, "failed", "ineligible")
                return "ineligible"
            if (
                repository.first_job(conn, story_id)[0] != "running"
                or repository.read_inputs(conn, story_id, first_only=True) != snapshot
            ):
                repository.finish_first_attempt(
                    conn, story_id, "pending", "stale", reset_attempts=attempt == MAX_FIRST_ATTEMPTS
                )
                return "stale"
            if codes is not None:
                repository.save_result(conn, story_id, codes)
                repository.finish_first_attempt(conn, story_id, "completed")
                return "completed"
            if reason == "temporary" and attempt < MAX_FIRST_ATTEMPTS:
                repository.finish_first_attempt(conn, story_id, "pending", reason)
            else:
                repository.finish_first_attempt(conn, story_id, "failed", reason)
                return "failed"


def classify_first(database_url: str, story_id: int, classify: Classifier) -> FirstOutcome:
    """커밋된 최초 분류 작업 하나를 실행한다. 세션 종료 시 작업 잠금도 해제된다."""
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            if not repository.try_lock_first(conn, story_id):
                return "busy"
            return _first_attempts(conn, story_id, classify)
    except psycopg.Error:
        logger.error("First category classification failed due to a database error")
        return "error"


def recover_first_jobs(database_url: str, classify: Classifier) -> dict[FirstOutcome, int]:
    """별도 작업 실행기가 대기 중이거나 중단된 최초 작업을 다시 맡는다."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        ids = repository.recoverable_first_ids(conn)
    return dict(Counter(classify_first(database_url, story_id, classify) for story_id in ids))


def run_hourly(
    run_once: Callable[[], CycleReport], stop: Event, *, interval_seconds: float = INTERVAL_SECONDS
) -> None:
    """독립 작업 프로세스에서 실행한다. 밀린 주기를 몰아서 실행하지 않는다."""
    if interval_seconds <= 0:
        raise ValueError("Category classification interval must be positive")
    deadline = monotonic() + interval_seconds
    while not stop.wait(max(0, deadline - monotonic())):
        try:
            report = run_once()
            logger.info(
                "Category cycle selected=%d completed=%d stale=%d failed=%d busy=%s",
                report.selected,
                report.completed,
                report.stale,
                report.failed,
                report.busy,
            )
        except psycopg.Error:
            logger.error("Category classification cycle failed due to a database error")
        deadline += interval_seconds
        now = monotonic()
        if now >= deadline:
            deadline += ((now - deadline) // interval_seconds + 1) * interval_seconds
