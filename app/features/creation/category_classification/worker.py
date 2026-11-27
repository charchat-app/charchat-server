"""CREATE-US-09 등록 후 확정 변경을 모아 처리하는 독립 분류 작업."""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

import psycopg

from app.features.creation.category_classification import repository

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 3600
# 이 DB의 카테고리 배치만 직렬화한다. 스토리 행 잠금과는 별개다.
CYCLE_LOCK = 0x43415409
MAX_FIRST_ATTEMPTS = 3
# 확정 변경 하나당 재분류 유료 시도 상한(재시작한 주기 포함 누적).
MAX_RECLASSIFY_ATTEMPTS = 3


class RetryableClassificationError(Exception):
    """다시 시도할 수 있는 분류기 오류."""


@dataclass(frozen=True)
class ClassificationInput:
    world_description: str | None
    character_descriptions: tuple[str | None, ...]


Classifier = Callable[[ClassificationInput], list[str] | tuple[str, ...]]
# 실행기 경계에서 새 작업·시도를 시작하기 전에 부른다. True면 멈춘다.
# 예외(예: 리더 연결 끊김)는 삼키지 않고 호출자에게 그대로 올린다.
StopCheck = Callable[[], bool]


@dataclass
class CycleReport:
    selected: int = 0
    completed: int = 0
    stale: int = 0
    failed: int = 0
    busy: bool = False
    stopped: bool = False


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


ReclassifyOutcome = Literal["completed", "stale", "failed", "stopped"]


@dataclass(frozen=True)
class _Reservation:
    attempt: int
    token: UUID
    snapshot: tuple


def _reserve_reclassification(
    conn, story_id, should_stop: StopCheck | None, previous: _Reservation | None
):
    """재분류 시도 하나를 기록한다. 끝난 결과(ReclassifyOutcome) 또는 _Reservation을 돌려준다.

    확정 저장과 같은 스토리 행 잠금 안에서 시도 기록·입력을 함께 읽는다. previous가 있으면
    즉시 재시도이며, 그 사이 기록(토큰)이나 입력이 바뀌었으면 옛 입력을 다시 보내지 않는다.
    마지막 확인(should_stop)은 시도 기록 직후 같은 트랜잭션 안에서 하고, 멈추거나 예외가 나면
    롤백으로 기록이 사라진다(_reserve_attempt와 같은 규약).
    """
    probe_error: Exception | None = None
    try:
        with conn.transaction():
            if not repository.lock_pending(conn, story_id):
                return "stale"
            row = repository.reclassification_attempt(conn, story_id)
            if previous is not None and (
                row is None or row[1] != previous.token or row[2] != "temporary"
            ):
                return "stale"
            if row is not None:
                attempts, _, reason = row
                if reason == "permanent":
                    return "failed"
                if attempts >= MAX_RECLASSIFY_ATTEMPTS:
                    # 상한에서 실행 중으로 남은 시도(비정상 종료)는 호출 없이 종료로 정리한다.
                    if reason is None:
                        repository.finish_reclassification(conn, story_id, "interrupted")
                    return "failed"
                # 상한 전 실행 중 기록은 끝나지 못한 시도다. 이미 센 시도로 두고 다음 시도로 간다.
            snapshot = repository.read_inputs(conn, story_id, pending_only=True)
            if previous is not None and snapshot != previous.snapshot:
                return "stale"
            token = uuid4()
            attempt = repository.reserve_reclassification(conn, story_id, token)
            try:
                if should_stop is not None and should_stop():
                    raise _DispatchCancelled
            except Exception as exc:
                probe_error = exc
                raise
    except Exception as exc:
        if probe_error is None:
            raise
        if isinstance(probe_error, _DispatchCancelled):
            return "stopped"
        if exc is probe_error:
            raise
        raise probe_error from None
    return _Reservation(attempt, token, snapshot)


def _reclassify(
    conn, story_id, classify, known_codes, should_stop: StopCheck | None
) -> ReclassifyOutcome:
    """확정 변경 하나에 대해 최대 MAX_RECLASSIFY_ATTEMPTS번(재시작 포함 누적) 유료 시도를 한다.

    RetryableClassificationError만 같은 주기 안에서 바로 재시도한다. 영구 실패·잘못된 출력은
    종료로 기록해 이후 주기에서 건너뛴다. 실패해도 분류 필요 표시와 기존 연결은 그대로 둔다.
    추론은 트랜잭션·행 잠금 밖에서 하고, 반영은 스토리 행을 잠근 뒤 입력과 예약 토큰이 모두
    예약 때와 같을 때만 한다. 관련 확정 변경은 기록을 지우므로 토큰이 달라진다(ABA 포함).
    """
    reservation: _Reservation | None = None
    while True:
        if reservation is not None and should_stop is not None and should_stop():
            return "stopped"
        reserved = _reserve_reclassification(conn, story_id, should_stop, reservation)
        if isinstance(reserved, str):
            return reserved
        reservation = reserved
        try:
            codes = _infer(classify, reservation.snapshot, known_codes, propagate_retryable=True)
            reason = "permanent"
        except RetryableClassificationError:
            codes, reason = None, "temporary"

        with conn.transaction():
            pending = repository.lock_pending(conn, story_id)
            row = repository.reclassification_attempt(conn, story_id)
            ours = row is not None and row[1] == reservation.token
            current = repository.read_inputs(conn, story_id, pending_only=True)
            if not pending or not ours or current != reservation.snapshot:
                # 관련 확정 변경이 기록을 지웠다면 새 변경은 다음 주기에 새 예산으로 처리한다.
                # 서비스 밖 변경으로 입력만 달라졌다면 이 시도를 결과 없는 시도로 남긴다.
                if ours:
                    repository.finish_reclassification(conn, story_id, "interrupted")
                return "stale"
            if codes is not None:
                repository.save_result(conn, story_id, codes)
                repository.reset_reclassification(conn, story_id)
                return "completed"
            repository.finish_reclassification(conn, story_id, reason)
            if reason == "permanent" or reservation.attempt >= MAX_RECLASSIFY_ATTEMPTS:
                return "failed"


def run_cycle(
    database_url: str, classify: Classifier, *, should_stop: StopCheck | None = None
) -> CycleReport:
    """한 주기의 대상을 한 번만 수집하고, 추론 동안 스토리 행 잠금을 잡지 않는다.

    should_stop은 스토리마다, 그리고 즉시 재시도 전에 확인하며 시도 기록 직후 같은 트랜잭션
    안에서 한 번 더 확인한다. True면 남은 스토리·시도를 다음 주기로 넘긴다(남은 예산 유지).
    """
    with psycopg.connect(database_url, autocommit=True) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (CYCLE_LOCK,)).fetchone()[0]:
            return CycleReport(busy=True)
        # 연결 종료 시 세션 잠금도 풀린다. 이 잠금 덕분에 주기 시작 시 실행 중으로 남은 재분류
        # 기록은 끝나지 못한 시도다. 추론은 트랜잭션 밖에서 실행한다.
        story_ids = repository.pending_ids(conn, MAX_RECLASSIFY_ATTEMPTS)
        known_codes = repository.category_codes(conn)
        report = CycleReport(selected=len(story_ids))
        for story_id in story_ids:
            if should_stop is not None and should_stop():
                report.stopped = True
                break
            outcome = _reclassify(conn, story_id, classify, known_codes, should_stop)
            if outcome == "stopped":
                report.stopped = True
                break
            if outcome == "completed":
                report.completed += 1
            elif outcome == "stale":
                report.stale += 1
            else:
                report.failed += 1
        return report


FirstOutcome = Literal["completed", "stale", "failed", "ineligible", "error", "busy", "stopped"]


class _DispatchCancelled(Exception):
    """시도 기록 직후 확인에서 멈출 때 기록 트랜잭션을 롤백시키는 내부 신호."""


def _reserve_attempt(conn, story_id, should_stop: StopCheck | None):
    """시도 하나를 기록한다. 끝난 결과(FirstOutcome) 또는 (시도 번호, 입력)을 돌려준다.

    마지막 확인(should_stop)은 시도 기록 바로 뒤, 같은 트랜잭션 안에서 한다. 멈추거나
    확인이 예외(리더 연결 끊김)를 올리면 커밋 전이므로 롤백으로 기록이 사라진다. 작업 연결도
    함께 끊겨 롤백을 보낼 수 없으면 서버가 커밋되지 않은 기록을 버린다. 이때 롤백 오류가
    확인 결과(멈춤·원래 예외)를 가리지 않게 한다.
    """
    probe_error: Exception | None = None
    try:
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
            # 행 잠금 대기 중에 들어온 종료·양보 요청과 리더 끊김도 여기서 잡는다.
            try:
                if should_stop is not None and should_stop():
                    raise _DispatchCancelled
            except Exception as exc:
                probe_error = exc
                raise
    except Exception as exc:
        if probe_error is None:
            raise
        if isinstance(probe_error, _DispatchCancelled):
            return "stopped"
        if exc is probe_error:
            raise
        raise probe_error from None
    return attempts + 1, snapshot


def _first_attempts(conn, story_id, classify, should_stop: StopCheck | None = None) -> FirstOutcome:
    """should_stop은 시도(즉시 재시도 포함)마다 두 번 확인한다.

    1) 스토리 행 잠금 전, 2) 시도 기록 직후 같은 트랜잭션 안(커밋 전). 2)에서 멈추거나 2)가
    예외를 올리면 기록은 롤백되고, 커밋 뒤에는 확인 없이 바로 공급자를 호출한다.
    커밋 응답과 공급자 호출 사이의 모호함은 없앨 수 없다. 커밋이 서버에 반영됐지만 응답을 받지
    못하면(DB 오류) 보내지 않은 시도가 쓴 것으로 남고, 커밋 뒤 비정상 종료나 작업 연결 끊김으로
    작업 잠금이 풀리면 공급자 호출이 나간 시도를 다른 실행기가 다시 시도할 수 있다. 시도 수는
    적게 세지 않는 쪽을 택하며, 공급자 호출의 정확히 한 번 실행은 보장하지 않는다.
    멈추면 남은 시도는 복구 때 이어서 쓴다.
    """
    known_codes = repository.category_codes(conn)
    while True:
        if should_stop is not None and should_stop():
            return "stopped"
        reserved = _reserve_attempt(conn, story_id, should_stop)
        if isinstance(reserved, str):
            return reserved
        attempt, snapshot = reserved
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


def classify_first(
    database_url: str,
    story_id: int,
    classify: Classifier,
    *,
    should_stop: StopCheck | None = None,
) -> FirstOutcome:
    """커밋된 최초 분류 작업 하나를 실행한다. 세션 종료 시 작업 잠금도 해제된다.

    should_stop이 없으면(API 등록 직후 경로) 기존처럼 상한까지 바로 재시도한다.
    should_stop이 올린 예외는 DB 오류로 바꾸지 않고 그대로 올린다.
    """
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            if not repository.try_lock_first(conn, story_id):
                return "busy"
            return _first_attempts(conn, story_id, classify, should_stop)
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
