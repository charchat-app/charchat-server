"""CREATE-US-09 등록 후 확정 변경을 모아 처리하는 독립 분류 작업."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic

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
            world, characters = snapshot
            value = ClassificationInput(world, tuple(description for _, description in characters))
            try:
                codes = _validate(classify(value), known_codes)
            except Exception:
                # 공급자 예외에 입력/응답이 포함될 수 있으므로 본문을 기록하지 않는다.
                codes = None
            if codes is None:
                report.failed += 1
                continue
            with conn.transaction():
                if not repository.lock_pending(conn, story_id):
                    report.stale += 1
                    continue
                # 모든 확정 기본 정보/캐릭터 쓰기가 같은 스토리 행을 먼저 잠근다.
                if repository.read_inputs(conn, story_id, pending_only=True) != snapshot:
                    report.stale += 1
                    continue
                repository.save_result(conn, story_id, codes)
            report.completed += 1
        return report


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
