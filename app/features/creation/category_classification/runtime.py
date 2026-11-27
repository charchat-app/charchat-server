"""CREATE-US-09 독립 분류 실행기(최초 작업 복구 + 시간 배치).

실행(명시적으로만): python -m app.features.creation.category_classification.runtime
FastAPI 서버와 별도 프로세스다. 서버 시작에는 연결하지 않는다.
"""

import argparse
import logging
import signal
from collections import Counter
from time import monotonic, sleep
from typing import Protocol

import psycopg

from app.core.config import settings
from app.features.creation.category_classification import repository, worker
from app.features.creation.category_classification.classifier import OpenRouterClassifier

logger = logging.getLogger(__name__)

# 실행기 리더 잠금. worker.CYCLE_LOCK(배치 직렬화)·스토리별 잠금(-story_id)과 다른 값이다.
SCHEDULER_LOCK = 0x4341540A
RECOVERY_INTERVAL_SECONDS = 15
HOURLY_INTERVAL_SECONDS = worker.INTERVAL_SECONDS
# CLI 종료 요청을 확인하는 최대 간격(초).
STOP_POLL_SECONDS = 1.0
JOBS_TABLE = "story_first_classification_jobs"
# 리더 연결 작업의 상한. 연결이 끊기면 실행기는 종료한다.
LEADER_CONNECT_KWARGS = {
    "autocommit": True,
    "connect_timeout": 10,
    "options": "-c statement_timeout=5000",
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 30000,
}
# 복구 대상 수집(연결 + 조회 1회)의 고정 상한. 시간 배치를 오래 늦추지 않기 위한 값이다.
# connect_timeout은 호스트 시도마다 적용되고 이름 해석은 제한하지 않으므로 최선 노력이다.
RECOVERY_CONNECT_TIMEOUT_SECONDS = 2
RECOVERY_STATEMENT_TIMEOUT_MS = 2000
RECOVERY_CONNECT_KWARGS = {
    "autocommit": True,
    "connect_timeout": RECOVERY_CONNECT_TIMEOUT_SECONDS,
    "options": f"-c statement_timeout={RECOVERY_STATEMENT_TIMEOUT_MS}",
}
# 다음 시간 배치가 이보다 가까우면 복구 수집을 배치 뒤로 미룬다.
RECOVERY_COLLECT_BOUND_SECONDS = (
    RECOVERY_CONNECT_TIMEOUT_SECONDS + RECOVERY_STATEMENT_TIMEOUT_MS / 1000
)


class StopSignal(Protocol):
    def is_set(self) -> bool: ...
    def wait(self, timeout: float) -> bool: ...


class StopFlag:
    """CLI 종료 표시. 시그널 처리기는 bool 하나만 바꾸고 잠금·로그를 쓰지 않는다.

    threading.Event.set()은 내부 잠금을 잡으므로, 같은 스레드에서 wait() 중에 처리기가
    실행되면 교착될 수 있다. 대기는 STOP_POLL_SECONDS 이하로 나눠 자며 표시를 확인한다.
    """

    def __init__(self) -> None:
        self.requested = False

    def request(self, _signum=None, _frame=None) -> None:
        self.requested = True

    def is_set(self) -> bool:
        return self.requested

    def wait(self, timeout: float) -> bool:
        end = monotonic() + timeout
        while not self.requested:
            remaining = end - monotonic()
            if remaining <= 0:
                return False
            sleep(min(remaining, STOP_POLL_SECONDS))
        return True


class LeaderConnectionLost(RuntimeError):
    """리더 잠금을 잡은 연결이 끊겼다. 잠금이 풀렸을 수 있으므로 실행을 멈춘다."""


class MissingJobsTable(RuntimeError):
    """최초 분류 작업 테이블이 없다. 마이그레이션 전 DB에서 계속 폴링하지 않고 종료한다."""


def _next_deadline(deadline: float, interval: float, now: float) -> float:
    """다음 마감을 정한다. 밀린 회차는 몰아서 실행하지 않고 건너뛴다."""
    deadline += interval
    if now >= deadline:
        deadline += ((now - deadline) // interval + 1) * interval
    return deadline


def _check_leader(leader: psycopg.Connection) -> None:
    try:
        leader.execute("SELECT 1").fetchone()
    except psycopg.Error:
        raise LeaderConnectionLost("Category scheduler leader connection was lost") from None


def _stop_check(stop: StopSignal, leader: psycopg.Connection) -> worker.StopCheck:
    """작업·시도 경계마다 종료 요청과 리더 연결을 확인한다.

    리더 연결이 끊기면 LeaderConnectionLost를 올린다(worker는 이를 삼키지 않는다).
    리더 확인 중에 들어온 종료 요청도 놓치지 않도록 확인 뒤 종료 표시를 다시 본다.
    이미 보낸 공급자 호출은 막지 못하므로 절대적인 펜싱은 아니다.
    """

    def should_stop() -> bool:
        if stop.is_set():
            return True
        _check_leader(leader)
        return stop.is_set()

    return should_stop


def _require_jobs_table(conn: psycopg.Connection) -> None:
    row = conn.execute("SELECT to_regclass(%s)", (JOBS_TABLE,)).fetchone()
    if row is None or row[0] is None:
        raise MissingJobsTable(f"{JOBS_TABLE} table is missing")


def _recover(
    database_url: str,
    classify: worker.Classifier,
    should_stop: worker.StopCheck,
    yield_at: float,
) -> None:
    """대기·중단 최초 작업을 하나씩 이어받는다.

    수집은 RECOVERY_CONNECT_KWARGS의 고정 상한 안에서 한 번 조회한다. 수집 뒤 작업마다,
    그리고 작업 안의 시도 경계마다 종료·리더와 시간 배치 마감(yield_at)을 확인하고, 마감이
    지났으면 남은 작업·시도를 다음 복구 회차로 넘긴다. 공급자 호출이 이미 나간 시도 하나는
    끝까지 기다리므로 시간 배치를 늦출 수 있다.
    """
    try:
        with psycopg.connect(database_url, **RECOVERY_CONNECT_KWARGS) as conn:
            ids = repository.recoverable_first_ids(conn)
    except psycopg.Error:
        logger.error("First classification recovery failed due to a database error")
        return

    def yield_or_stop() -> bool:
        # 리더 확인(should_stop) 동안 마감이 지날 수 있어 앞뒤로 마감을 본다.
        if monotonic() >= yield_at or should_stop():
            return True
        return monotonic() >= yield_at

    outcomes: Counter = Counter()
    for story_id in ids:
        if yield_or_stop():
            break
        outcome = worker.classify_first(database_url, story_id, classify, should_stop=yield_or_stop)
        outcomes[outcome] += 1
        if outcome == "stopped":
            break
    if outcomes:
        logger.info(
            "First classification recovery %s",
            " ".join(f"{key}={value}" for key, value in sorted(outcomes.items())),
        )


def _hourly(database_url: str, classify: worker.Classifier, should_stop: worker.StopCheck) -> None:
    try:
        report = worker.run_cycle(database_url, classify, should_stop=should_stop)
    except psycopg.Error:
        logger.error("Category classification cycle failed due to a database error")
        return
    logger.info(
        "Category cycle selected=%d completed=%d stale=%d failed=%d busy=%s stopped=%s",
        report.selected,
        report.completed,
        report.stale,
        report.failed,
        report.busy,
        report.stopped,
    )


def run_worker(database_url: str, classify: worker.Classifier, stop: StopSignal) -> bool:
    """리더 잠금을 잡고 stop까지 실행한다. 다른 실행기가 잠금을 잡고 있으면 바로 False.

    복구는 시작 직후와 이후 15초마다, 시간 배치는 시작 시점 기준 3,600초마다 실행한다.
    작업은 순서대로 하나씩 실행하며, 밀린 회차는 몰아서 실행하지 않고 건너뛴다.
    마감된 시간 배치는 복구 수집보다 먼저 실행한다. 다음 시간 배치까지 남은 시간이 복구 수집
    상한보다 짧으면 수집을 배치 직후로 미룬다.
    작업 테이블이 없으면 MissingJobsTable, 리더 연결이 끊기면 LeaderConnectionLost를 올린다.
    """
    with psycopg.connect(database_url, **LEADER_CONNECT_KWARGS) as leader:
        _require_jobs_table(leader)
        locked = leader.execute("SELECT pg_try_advisory_lock(%s)", (SCHEDULER_LOCK,)).fetchone()
        if not locked[0]:
            return False
        # 연결이 닫히면 세션 잠금도 풀린다.
        should_stop = _stop_check(stop, leader)
        start = monotonic()
        recovery_at = start
        hourly_at = start + HOURLY_INTERVAL_SECONDS
        while not should_stop():
            # 루프를 다시 돌 때마다 종료·리더를 확인하고, 마감된 시간 배치를 먼저 본다.
            if monotonic() >= hourly_at:
                _hourly(database_url, classify, should_stop)
                hourly_at = _next_deadline(hourly_at, HOURLY_INTERVAL_SECONDS, monotonic())
                continue
            if monotonic() >= recovery_at:
                if hourly_at - monotonic() < RECOVERY_COLLECT_BOUND_SECONDS:
                    # 수집이 시간 배치를 늦추지 않도록 배치 직후로 미룬다.
                    recovery_at = hourly_at
                else:
                    _recover(database_url, classify, should_stop, hourly_at)
                    recovery_at = _next_deadline(
                        recovery_at, RECOVERY_INTERVAL_SECONDS, monotonic()
                    )
                continue
            delay = min(recovery_at, hourly_at) - monotonic()
            if delay > 0 and stop.wait(delay):
                break
        return True


def _blank(value) -> bool:
    if value is None:
        return True
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return not isinstance(value, str) or not value.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CREATE-US-09 카테고리 분류 실행기(최초 작업 복구 15초, 시간 배치 3,600초)."
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    # 오류 문구에는 설정 이름만 적고 값은 싣지 않는다.
    if _blank(settings.database_url):
        parser.error("DATABASE_URL을 설정하세요.")
    model = settings.category_openrouter_model
    api_key = settings.category_openrouter_api_key
    if _blank(model) or _blank(api_key):
        parser.error("CATEGORY_OPENROUTER_MODEL과 CATEGORY_OPENROUTER_API_KEY를 설정하세요.")
    classify = OpenRouterClassifier(model=model.strip(), api_key=api_key.get_secret_value())

    stop = StopFlag()
    signal.signal(signal.SIGINT, stop.request)
    signal.signal(signal.SIGTERM, stop.request)
    try:
        if not run_worker(settings.database_url, classify, stop):
            logger.error("Another category scheduler already holds the lock for this database")
            return 2
    except MissingJobsTable:
        logger.error("Category scheduler requires the %s table; apply the migration", JOBS_TABLE)
        return 1
    except LeaderConnectionLost:
        logger.error("Category scheduler leader connection was lost; exiting")
        return 1
    except psycopg.Error:
        logger.error("Category scheduler failed due to a database error")
        return 1
    except Exception as exc:
        # 예외 메시지에 접속 정보·입력이 들어갈 수 있어 종류만 기록한다.
        logger.error("Category scheduler failed: %s", type(exc).__name__)
        return 1
    logger.info("Category scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
