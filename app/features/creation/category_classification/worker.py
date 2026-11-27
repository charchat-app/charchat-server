"""CREATE-US-09 등록 후 확정 변경을 모아 처리하는 독립 분류 작업."""

import logging
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


def _infer(classify: Classifier, snapshot, known_codes: set[str]) -> tuple[str, ...] | None:
    """트랜잭션·행 잠금 밖에서 호출한다. 실패·잘못된 출력은 None."""
    world, characters = snapshot
    value = ClassificationInput(world, tuple(description for _, description in characters))
    try:
        return _validate(classify(value), known_codes)
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


FirstOutcome = Literal["completed", "stale", "failed", "ineligible", "error"]


def classify_first(database_url: str, story_id: int, classify: Classifier) -> FirstOutcome:
    """최초 등록 직후 한 스토리만 분류한다. 요청 커넥션과 별도의 새 연결을 쓴다.

    유효 결과가 아직 없고 분류 필요로 표시된 활성 등록 스토리만 다룬다. 실패·잘못된 출력·
    분류 중 확정본 변경(stale)은 표시와 기존 상태를 그대로 둔다. 다시 시도하지 않는다.
    """
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            snapshot = repository.read_inputs(conn, story_id, first_only=True)
            if snapshot is None:
                return "ineligible"
            known_codes = repository.category_codes(conn)
            codes = _infer(classify, snapshot, known_codes)
            if codes is None:
                logger.warning("First category classification failed story_id=%d", story_id)
                return "failed"
            if _save_if_current(conn, story_id, snapshot, codes, first_only=True):
                return "completed"
            logger.info("First category classification was stale story_id=%d", story_id)
            return "stale"
    except psycopg.Error:
        logger.error("First category classification failed due to a database error")
        return "error"


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
