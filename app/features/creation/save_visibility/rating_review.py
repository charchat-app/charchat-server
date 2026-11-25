"""CREATE-US-08-AC02·AC14 UNSAFE 확정 저장 뒤 등급 검토 넘김(선택, 기본 꺼짐).

- 비공개·공개 「저장」이 커밋되면 그 저장본을 조정기에 알린다. 저장본 등급 선택이 UNSAFE면
  검토 비교 내용을 백그라운드 평가기로 넘기고, SAFE이거나 내용을 읽지 못했으면(None) 앞서
  넘긴 검토만 무효로 한다. 요청은 평가를 기다리지 않는다.
- 커밋과 알림 사이 경쟁: 확정 저장은 스토리 잠금 안, COMMIT 직전 마지막 단계에 조정기
  울타리(`fence`)에 들어가고, 트랜잭션이 끝난 뒤 울타리를 나온다. 커밋에 성공했을 때만
  가장 최근 세대와 내용을 알린다(커밋 실패는 앞선 검토를 무효로 하지 않는다). 작업자는
  평가 시작 전과 통과 기록 전에 그 스토리의 울타리가 닫힐 때까지 기다린 뒤 자기 내용이
  아직 최신인지 확인한다. 평가 중에는 어떤 잠금도 쥐지 않는다.
- 단일 프로세스 전제다. 여러 프로세스·서버 사이의 순서는 보장하지 않는다.
- 평가기는 주입한다. 마지막 통과 기록은 이 프로세스 메모리에만 두며(재시작하면 사라진다)
  조정기 잠금 안에서 읽고 쓴다. 실제 공급자·영속 보관·지연·실패·재시도 정책은
  미정(상세 문서 2326529)이므로 운영 앱은 조정기 없이 만들어 넘기지 않는다.
- 결과 보관소(`results`)를 주지 않으면 DB에 검토 결과를 쓰지 않고 적용 등급·검토 상태도 바꾸지
  않는다(기존 동작). 보관소는 테스트·수동 주입에서만 준다(아래 「검토 결과 저장」).
- 스토리 원문을 로그에 남기지 않는다(스토리 ID·세대·예외 종류만).

검토 결과 저장(CREATE-US-08-AC02·AC03·AC09~AC11, `results`를 줄 때만):
- 확정 저장 트랜잭션 안, 응답을 만들기 전에 UNSAFE면 검토 중(`reviewing`)과 새 요청 표
  (`rating_requested_at`)를 쓴다. 적용 등급은 그대로 둔다(재검토 중에는 앞선 UNSAFE 유지).
  SAFE면 적용 등급을 SAFE·검토 상태를 not_reviewed로 둬 대기 중인 검토를 무효로 한다. 요청 표는
  마지막 요청 시각으로 남겨 다음 요청 표가 늘 더 크게 한다(앞선 표를 다시 쓰지 않는다).
- 작업자는 평가를 마친 뒤 조정기 잠금 밖에서 독립 DB 연결로 스토리를 잠그고, 요청 표가 그대로이고
  UNSAFE 선택·검토 중·삭제되지 않았으며 정본 내용이 평가한 내용과 같을 때만 결과를 쓴다.
- 적격은 UNSAFE·완료, 확인된 부적격·판단 불가는 SAFE·완료(사유 코드 기록), 평가 실패·잘못된
  출력·넘김 실패는 앞선 적용 등급을 유지한 채 실패로 기록한다(부적격이 아니다). 사유는 허용
  목록 코드만 쓰고 평가기 원문은 쓰지 않는다. 새 재시도 정책은 없다(다음 확정 저장이 다시 넘긴다).
- 이 경로는 프로세스 메모리 통과 기록·중복 제거를 쓰지 않는다. 메모리 기록은 영속하지 않고 다른
  경로로 정본이 바뀌었는지 알 수 없어 잘못 재사용할 수 있으므로, UNSAFE 확정 저장마다 새로
  검토한다. 앞선 요청의 평가는 요청 표가 달라 결과를 쓰지 못한다.
- 남은 한계: 재시작·`close()`로 대기·평가 중 검토가 사라지면 그 스토리는 다음 확정 저장까지
  `reviewing`(앞선 적용 등급 유지)으로 남는다. 다른 경로로 정본이 바뀌어 내용이 어긋난 결과도
  쓰지 않으므로 같은 상태로 남는다. 영속 통과 기록(재시작 뒤 재사용)·재시도·복구 작업은 미정이다.
"""

import itertools
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Protocol

import psycopg
from psycopg.rows import dict_row

from app.features.creation.initial_state import service as initial_state
from app.features.creation.save_visibility import repository
from app.features.creation.save_visibility.review_verdict import (
    Eligibility,
    EligibilityReason,
    EligibilityVerdict,
    revalidated,
    unavailable,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewSnapshot:
    """AC14 검토 비교 내용. 내용이 같으면 같다(요청 원문 해시가 아니다).

    원문을 담으므로 repr에 싣지 않는다.
    """

    world_description: str | None = field(repr=False)
    # (캐릭터 id, 이름, 상세) id 순. 세계관·상세·관계가 이름으로 인물을 가리키므로 이름도
    # 인물 정체로 비교한다. 그 밖의 필드는 비교하지 않는다.
    characters: tuple = field(repr=False)
    # (출발 종류, 출발 id, 대상 종류, 대상 id, 라벨) 정렬. 라벨이 빈 관계는 뺀다.
    relations: tuple = field(repr=False)


def build_snapshot(world_description, characters, relations) -> ReviewSnapshot:
    people = tuple(sorted((c["id"], c["name"], c["description"]) for c in characters))
    links = tuple(
        sorted(
            (
                r["source_kind"],
                r["source_character_id"] or 0,
                r["target_kind"],
                r["target_character_id"] or 0,
                r["label"],
            )
            for r in relations
            if r["label"] is not None and r["label"].strip()
        )
    )
    return ReviewSnapshot(world_description, people, links)


def capture_snapshot(connection, story_id) -> ReviewSnapshot:
    """같은 트랜잭션 안에서 이번 저장이 쓴 정본을 읽는다."""
    return build_snapshot(
        repository.read_basic(connection, story_id)["world_description"],
        repository.read_characters(connection, story_id),
        initial_state.read_relations(connection, story_id),
    )


class ReviewEvaluator(Protocol):
    def evaluate(self, story_id: int, snapshot: ReviewSnapshot) -> EligibilityVerdict:
        """검증된 적격 판정만 통과다(`is_eligible`). 그 밖의 값·예외는 통과가 아니다.

        조정기 잠금 밖에서 부른다.
        """


class LastPassStore(Protocol):
    """마지막 통과 기록. 조정기 잠금 안에서 부르므로 막히지 않는 프로세스 안 구현만 허용한다.

    네트워크·DB 등 외부 IO가 필요한 영속 보관소로 바꿀 때는 이 프로토콜을 그대로 쓰지 말고
    잠금 밖에서 읽고 쓰도록 조정기 구조를 함께 바꾼다.
    """

    def get(self, story_id: int) -> ReviewSnapshot | None: ...

    def set(self, story_id: int, snapshot: ReviewSnapshot) -> None: ...


class ProcessLastPassStore:
    """프로세스 메모리 통과 기록(비영속). 조정기 잠금이 동기화를 맡는다."""

    def __init__(self):
        self.passes: dict[int, ReviewSnapshot] = {}

    def get(self, story_id):
        return self.passes.get(story_id)

    def set(self, story_id, snapshot):
        self.passes[story_id] = snapshot


@dataclass(frozen=True)
class ReviewResult:
    """DB에 쓸 검토 결과. `review_result`·`FAILURE`만 만든다(허용 목록 값)."""

    effective_rating: str | None  # None이면 앞선 적용 등급을 유지한다.
    status: str
    reason: str | None


_OUTCOME_TARGETS = {
    Eligibility.ELIGIBLE: ("UNSAFE", "completed"),
    Eligibility.INELIGIBLE: ("SAFE", "completed"),
    Eligibility.UNDETERMINED: ("SAFE", "completed"),
    Eligibility.UNAVAILABLE: (None, "failed"),
}


def encode_reason(verdict: EligibilityVerdict) -> str | None:
    """검증된 판정을 `<판정>:<사유>,<사유>` 코드로 바꾼다. 적격은 사유가 없다(None)."""
    if verdict.outcome is Eligibility.ELIGIBLE:
        return None
    return f"{verdict.outcome.value}:{','.join(sorted(r.value for r in verdict.reasons))}"


def decode_reason(text) -> EligibilityVerdict | None:
    """저장된 사유 코드를 허용 목록으로 다시 검증한다. 맞지 않으면 None(그대로 내보내지 않는다)."""
    if not isinstance(text, str):
        return None
    outcome, sep, codes = text.partition(":")
    try:
        if not sep or outcome == Eligibility.ELIGIBLE:
            return None
        return EligibilityVerdict(
            outcome=Eligibility(outcome),
            reasons=frozenset(EligibilityReason(code) for code in codes.split(",")),
        )
    except ValueError:
        return None


def reason_payload(text) -> dict | None:
    """응답 `rating_review_reason` 값. 허용 목록으로 다시 검증한 코드만, 아니면 None."""
    verdict = decode_reason(text)
    if verdict is None:
        return None
    return {"outcome": verdict.outcome.value, "reasons": sorted(r.value for r in verdict.reasons)}


def review_result(verdict) -> ReviewResult:
    """평가기 반환값을 엄격히 다시 검증해 결과로 바꾼다. 잘못된 값은 실패(`invalid_signals`)다."""
    checked = revalidated(EligibilityVerdict, verdict)
    if checked is None:
        checked = unavailable(EligibilityReason.INVALID_SIGNALS)
    effective, status = _OUTCOME_TARGETS[checked.outcome]
    return ReviewResult(effective, status, encode_reason(checked))


# 평가기 예외·넘김 실패·울타리 대기 시간 초과. 앞선 적용 등급을 유지한다.
FAILURE = ReviewResult(None, "failed", encode_reason(unavailable()))


class ReviewResults(Protocol):
    """검토 결과 보관소. 조정기 잠금 밖(작업자 또는 커밋 뒤 요청 스레드)에서 부른다."""

    def apply(
        self,
        story_id: int,
        snapshot: ReviewSnapshot,
        ticket: datetime,
        result: ReviewResult,
    ) -> bool:
        """요청 표와 정본 내용이 그대로일 때만 쓰고, 썼는지 돌려준다.

        현재 검토 상태(stories)만 쓴다. 요청키 영수증(재전송 응답)은 커밋 시점 값으로 고정되어
        바꾸지 않는다.
        """


class DatabaseReviewResults:
    """요청과 독립된 새 DB 연결로 결과를 쓴다. 접속 정보는 주입받는다(환경변수를 읽지 않는다)."""

    def __init__(self, conninfo: str):
        self._conninfo = conninfo

    def __repr__(self):
        return "DatabaseReviewResults(...)"  # 접속 정보를 싣지 않는다.

    def apply(self, story_id, snapshot, ticket, result) -> bool:
        with psycopg.connect(self._conninfo, autocommit=True, row_factory=dict_row) as connection:
            with connection.transaction():
                row = repository.lock_review_target(connection, story_id)
                if (
                    row is None
                    or row["deleted_at"] is not None
                    or row["requested_rating"] != "UNSAFE"
                    or row["rating_review_status"] != "reviewing"
                    or row["rating_requested_at"] != ticket
                    or capture_snapshot(connection, story_id) != snapshot
                ):
                    return False
                return repository.apply_review_result(
                    connection,
                    story_id,
                    ticket,
                    result.effective_rating,
                    result.status,
                    result.reason,
                )


@dataclass
class Publication:
    """울타리 한 번의 결과. `outcome`은 울타리를 나온 뒤 채워진다."""

    generation: int
    outcome: str | None = None


class RatingReviewCoordinator:
    """프로세스 안 백그라운드 검토 조정기. 상태는 메모리에만 있어 재시작하면 사라진다."""

    def __init__(
        self,
        evaluator: ReviewEvaluator,
        store: LastPassStore | None = None,
        max_workers: int = 1,
        fence_timeout: float = 30.0,
        results: ReviewResults | None = None,
    ):
        self._evaluator = evaluator
        self._results = results
        self._store = store if store is not None else ProcessLastPassStore()
        self._executor = ThreadPoolExecutor(max_workers, thread_name_prefix="rating-review")
        self._fence_timeout = fence_timeout
        # 아래 상태는 모두 이 조건 변수(RLock)로 보호한다.
        self._cond = threading.Condition()
        self._generations = itertools.count(1)
        # 스토리별 마지막으로 알린 (세대, 내용). 내용 None은 검토 대상이 아님(SAFE·읽기 실패).
        self._latest: dict[int, tuple[int, ReviewSnapshot | None]] = {}
        # 스토리별 열린 울타리 수(커밋 중인 확정 저장)
        self._fenced: dict[int, int] = {}
        # 스토리별 맡은(대기·평가 중) 내용 → 맡긴 세대
        self._active: dict[int, dict[ReviewSnapshot, int]] = {}
        self._futures: set[Future] = set()

    @property
    def persists(self) -> bool:
        """검토 결과를 DB에 쓰는가(결과 보관소를 줬는가)."""
        return self._results is not None

    @contextmanager
    def fence(
        self,
        story_id: int,
        snapshot: ReviewSnapshot | None,
        request_ticket: datetime | None = None,
    ):
        """확정 저장 트랜잭션의 마지막 단계(스토리 잠금 안, COMMIT 직전)에 들어간다.

        트랜잭션을 감싼 바깥 문맥에서 나오므로 COMMIT이 끝난 뒤 나온다. 예외 없이 나올 때만
        세대·내용을 알리고, 예외(커밋·저장 실패)면 알리지 않는다. 알림 실패는 로그만 남긴다.
        `request_ticket`은 결과 저장 경로에서 이번 저장이 쓴 요청 표다.
        """
        with self._cond:
            ticket = Publication(next(self._generations))
            self._fenced[story_id] = self._fenced.get(story_id, 0) + 1
        committed = False
        try:
            yield ticket
            committed = True
        finally:
            with self._cond:
                try:
                    if committed:
                        ticket.outcome = self._publish(
                            story_id, ticket.generation, snapshot, request_ticket
                        )
                except Exception as exc:
                    ticket.outcome = "failed"
                    logger.warning(
                        "등급 검토 알림 실패 story_id=%s generation=%s error=%s",
                        story_id,
                        ticket.generation,
                        type(exc).__name__,
                    )
                finally:
                    remaining = self._fenced[story_id] - 1
                    if remaining:
                        self._fenced[story_id] = remaining
                    else:
                        del self._fenced[story_id]
                    self._cond.notify_all()

    def publish(self, story_id: int, snapshot: ReviewSnapshot | None) -> str:
        """DB 트랜잭션 없이 바로 알린다(울타리에 들어갔다 곧바로 나온다)."""
        with self.fence(story_id, snapshot) as ticket:
            pass
        return ticket.outcome

    def _publish(self, story_id, generation, snapshot, request_ticket=None) -> str:
        """조정기 잠금 안에서 부른다. 외부 IO 없이 끝난다."""
        latest = self._latest.get(story_id)
        if latest is not None and generation <= latest[0]:
            return "stale"  # 더 나중에 커밋된 저장이 이미 알려졌다.
        self._latest[story_id] = (generation, snapshot)
        if snapshot is None:
            return "invalidated"
        if request_ticket is not None:
            # 결과 저장 경로는 메모리 통과 기록·중복 제거를 쓰지 않고 요청 표마다 평가한다.
            return self._submit(story_id, snapshot, generation, request_ticket)
        try:
            passed = self._store.get(story_id) == snapshot
        except Exception as exc:
            # 통과 여부를 모르면 통과로 보지 않고 다시 검토한다.
            logger.warning(
                "등급 검토 통과 기록 읽기 실패 story_id=%s error=%s", story_id, type(exc).__name__
            )
            passed = False
        if passed:
            return "already_passed"
        active = self._active.setdefault(story_id, {})
        if snapshot in active:
            return "deduplicated"
        try:
            future = self._executor.submit(self._run, story_id, snapshot, generation)
        except Exception as exc:
            if not active:
                del self._active[story_id]
            logger.warning(
                "등급 검토 넘김 실패 story_id=%s generation=%s error=%s",
                story_id,
                generation,
                type(exc).__name__,
            )
            return "failed"
        # 작업자는 첫 단계에서 이 잠금을 기다리므로 아래 기록보다 먼저 진행하지 않는다.
        active[snapshot] = generation
        self._futures.add(future)
        # RLock이라 이미 끝난 future의 콜백이 여기서 바로 불려도 막히지 않는다.
        future.add_done_callback(partial(self._finished, story_id, snapshot, generation))
        return "submitted"

    def _submit(self, story_id, snapshot, generation, request_ticket) -> str:
        """결과 저장 경로의 넘김. 조정기 잠금 안에서 부른다."""
        try:
            future = self._executor.submit(
                self._run, story_id, snapshot, generation, request_ticket
            )
        except Exception as exc:
            logger.warning(
                "등급 검토 넘김 실패 story_id=%s generation=%s error=%s",
                story_id,
                generation,
                type(exc).__name__,
            )
            return "failed"
        self._futures.add(future)
        future.add_done_callback(partial(self._finished, story_id, snapshot, generation))
        return "submitted"

    def _record(self, story_id, snapshot, generation, request_ticket, result) -> bool:
        """조정기 잠금 밖에서 부른다. 요청 표·정본 내용이 그대로일 때만 결과가 쓰인다."""
        try:
            applied = self._results.apply(story_id, snapshot, request_ticket, result)
        except Exception as exc:
            logger.warning(
                "등급 검토 결과 기록 실패 story_id=%s generation=%s error=%s",
                story_id,
                generation,
                type(exc).__name__,
            )
            return False
        if not applied:
            logger.info("등급 검토 결과 버림 story_id=%s generation=%s", story_id, generation)
        return applied

    def record_handoff(self, story_id, snapshot, request_ticket, publication) -> None:
        """커밋 뒤, 울타리를 나온 뒤 요청 스레드에서 부른다. 넘기지 못했으면 실패로 기록한다.

        커밋되지 않았으면(`outcome` 없음) 아무것도 하지 않는다. 더 나중 저장이 먼저 알려졌으면
        요청 표가 달라 쓰이지 않는다. 현재 검토 상태만 쓰고, 요청키 영수증과 이미 만든 응답은
        커밋 시점 값(검토 중)으로 두며 바꾸지 않는다(CREATE-US-08-AC02·AC11).
        """
        if publication is None or publication.outcome in (None, "submitted"):
            return
        self._record(story_id, snapshot, publication.generation, request_ticket, FAILURE)

    def _finished(self, story_id, snapshot, generation, future):
        with self._cond:
            self._futures.discard(future)
            if future.cancelled():
                self._release(story_id, snapshot, generation)
            self._cond.notify_all()

    def _release(self, story_id, snapshot, generation):
        active = self._active.get(story_id)
        if active is not None and active.get(snapshot) == generation:
            del active[snapshot]
            if not active:
                del self._active[story_id]

    def _is_current(self, story_id, snapshot) -> bool:
        """잠금 안에서 부른다. 커밋 중인 울타리가 닫히기를 기다린 뒤 최신 내용인지 본다."""
        settled = self._cond.wait_for(
            lambda: not self._fenced.get(story_id), timeout=self._fence_timeout
        )
        if not settled:
            logger.warning("등급 검토 울타리 대기 시간 초과 story_id=%s", story_id)
            return False
        latest = self._latest.get(story_id)
        return latest is not None and latest[1] == snapshot

    def _run(self, story_id, snapshot, generation, request_ticket=None):
        released = False
        try:
            with self._cond:
                current = self._is_current(story_id, snapshot)
                if not current:
                    # 대기 중에 더 나중 저장이 커밋됐다(또는 울타리 대기 시간 초과).
                    # 평가하지 않는다.
                    self._release(story_id, snapshot, generation)
                    released = True
            if not current:
                if request_ticket is not None:
                    # 이 요청 표가 아직 최신이면(시간 초과) 검토 중으로 남기지 않고 실패로 둔다.
                    self._record(story_id, snapshot, generation, request_ticket, FAILURE)
                return
            outcome = FAILURE
            try:
                result = self._evaluator.evaluate(story_id, snapshot)
                outcome = review_result(result)
                checked = revalidated(EligibilityVerdict, result)
                passed = checked is not None and checked.outcome is Eligibility.ELIGIBLE
                if not passed:
                    # 검증된 판정의 종류·사유 코드만 남긴다. 잘못된 값은 필드·타입 이름도 읽지 않고
                    # 고정 코드만 남긴다(원문·평가기 출력이 섞일 수 있다).
                    logger.info(
                        "등급 검토 미통과 story_id=%s generation=%s outcome=%s reasons=%s",
                        story_id,
                        generation,
                        checked.outcome.value if checked is not None else "invalid",
                        sorted(r.value for r in checked.reasons) if checked is not None else [],
                    )
            except Exception as exc:
                logger.warning(
                    "등급 검토 실패 story_id=%s generation=%s error=%s",
                    story_id,
                    generation,
                    type(exc).__name__,
                )
                passed = False
                outcome = FAILURE
            if request_ticket is not None:
                # 조정기 잠금 밖에서 독립 연결로 쓴다.
                self._record(story_id, snapshot, generation, request_ticket, outcome)
            with self._cond:
                # 판정 기록과 맡은 내용 해제를 한 번에 한다.
                try:
                    # 실패·미통과는 예전 통과를 그대로 둔다. 다른 내용이 이미 커밋됐으면 버린다.
                    if passed and self._is_current(story_id, snapshot):
                        self._store.set(story_id, snapshot)
                except Exception as exc:
                    logger.warning(
                        "등급 검토 통과 기록 실패 story_id=%s generation=%s error=%s",
                        story_id,
                        generation,
                        type(exc).__name__,
                    )
                finally:
                    self._release(story_id, snapshot, generation)
                    released = True
        finally:
            if not released:
                with self._cond:
                    self._release(story_id, snapshot, generation)

    def wait(self, timeout: float | None = None) -> bool:
        """지금까지 넘긴 검토(기다리는 중 넘긴 것 포함)가 끝날 때까지 기다린다(테스트·종료용)."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._futures, timeout=timeout)

    def close(self) -> None:
        """대기 중인 검토는 취소하고, 평가 중인 검토는 기다리지 않는다."""
        self._executor.shutdown(wait=False, cancel_futures=True)


def capture(connection, story_id) -> ReviewSnapshot | None:
    """확정 저장 트랜잭션 안(스토리 잠금 중), 모든 쓰기를 마친 뒤 부른다.

    저장본 등급 선택이 UNSAFE면 검토 비교 내용을, 아니면 None을 돌려준다. 읽기 오류는
    세이브포인트로 되돌리고 None을 돌려줘 저장 자체는 실패시키지 않는다(None도 앞선 검토를
    무효로 한다).
    """
    try:
        with connection.transaction():
            if repository.read_rating(connection, story_id)["requested_rating"] != "UNSAFE":
                return None
            return capture_snapshot(connection, story_id)
    except Exception as exc:
        logger.warning(
            "등급 검토 내용 수집 실패 story_id=%s error=%s", story_id, type(exc).__name__
        )
        return None


@dataclass(frozen=True)
class FinalSaveHandoff:
    snapshot: ReviewSnapshot | None = field(repr=False)
    request_ticket: datetime | None = None


def prepare_final_save(
    connection, story_id, coordinator: RatingReviewCoordinator
) -> FinalSaveHandoff:
    """확정 저장 트랜잭션 안(스토리 잠금 중), 모든 쓰기 뒤·응답을 만들기 전에 부른다.

    결과 저장 경로면 UNSAFE는 검토 중·새 요청 표를, SAFE는 적용 등급 SAFE를 같은 트랜잭션에
    쓴다. UNSAFE인데 내용을 읽지 못했으면 넘길 수 없으므로 실패로 쓴다(적용 등급 유지).
    """
    snapshot = capture(connection, story_id)
    if not coordinator.persists:
        return FinalSaveHandoff(snapshot)
    if snapshot is not None:
        return FinalSaveHandoff(snapshot, repository.mark_review_pending(connection, story_id))
    if repository.read_rating(connection, story_id)["requested_rating"] == "UNSAFE":
        repository.mark_review_failed(connection, story_id, FAILURE.reason)
    else:
        repository.mark_rating_safe(connection, story_id)
    return FinalSaveHandoff(None)


def fence_final_save(
    story_id,
    coordinator: RatingReviewCoordinator,
    after_commit,
    handoff: FinalSaveHandoff,
):
    """확정 저장 트랜잭션의 마지막 단계로 울타리에 들어간다. 이 뒤로는 COMMIT 말고 SQL이 없다.

    `after_commit`은 트랜잭션을 감싼 `ExitStack`이다. 울타리는 커밋이 끝난 뒤 `after_commit`이
    닫힐 때 나온다. 결과 저장 경로면 그 뒤(조정기 잠금 밖) 넘기지 못한 요청을 현재 검토 상태에
    실패로 기록한다(같은 요청 표일 때만). 요청키 영수증과 이번 응답은 커밋 시점 값 그대로다.
    """
    entered = {}
    if handoff.request_ticket is not None:

        def record():
            coordinator.record_handoff(
                story_id, handoff.snapshot, handoff.request_ticket, entered.get("publication")
            )

        # ExitStack은 거꾸로 닫히므로 울타리를 나온 뒤에 불린다.
        after_commit.callback(record)
    entered["publication"] = after_commit.enter_context(
        coordinator.fence(story_id, handoff.snapshot, handoff.request_ticket)
    )
