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
- 이 모듈은 DB에 검토 결과를 쓰지 않고 적용 등급·검토 상태도 바꾸지 않는다.
- 스토리 원문을 로그에 남기지 않는다(스토리 ID·세대·예외 종류만).
"""

import itertools
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol

from app.features.creation.initial_state import service as initial_state
from app.features.creation.save_visibility import repository

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
    def evaluate(self, story_id: int, snapshot: ReviewSnapshot) -> bool:
        """True만 통과다. False·예외는 통과가 아니다. 조정기 잠금 밖에서 부른다."""


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
    ):
        self._evaluator = evaluator
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

    @contextmanager
    def fence(self, story_id: int, snapshot: ReviewSnapshot | None):
        """확정 저장 트랜잭션의 마지막 단계(스토리 잠금 안, COMMIT 직전)에 들어간다.

        트랜잭션을 감싼 바깥 문맥에서 나오므로 COMMIT이 끝난 뒤 나온다. 예외 없이 나올 때만
        세대·내용을 알리고, 예외(커밋·저장 실패)면 알리지 않는다. 알림 실패는 로그만 남긴다.
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
                        ticket.outcome = self._publish(story_id, ticket.generation, snapshot)
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

    def _publish(self, story_id, generation, snapshot) -> str:
        """조정기 잠금 안에서 부른다. 외부 IO 없이 끝난다."""
        latest = self._latest.get(story_id)
        if latest is not None and generation <= latest[0]:
            return "stale"  # 더 나중에 커밋된 저장이 이미 알려졌다.
        self._latest[story_id] = (generation, snapshot)
        if snapshot is None:
            return "invalidated"
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

    def _run(self, story_id, snapshot, generation):
        released = False
        try:
            with self._cond:
                if not self._is_current(story_id, snapshot):
                    # 대기 중에 더 나중 저장이 커밋됐다. 평가하지 않는다.
                    self._release(story_id, snapshot, generation)
                    released = True
                    return
            try:
                passed = self._evaluator.evaluate(story_id, snapshot) is True
            except Exception as exc:
                logger.warning(
                    "등급 검토 실패 story_id=%s generation=%s error=%s",
                    story_id,
                    generation,
                    type(exc).__name__,
                )
                passed = False
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


def fence_final_save(connection, story_id, coordinator: RatingReviewCoordinator, after_commit):
    """확정 저장 트랜잭션의 마지막 SQL로 내용을 읽고 울타리에 들어간다.

    `after_commit`은 트랜잭션을 감싼 `ExitStack`이다. 이 호출 뒤에는 COMMIT 말고 SQL이 없어야
    한다. 울타리는 커밋이 끝난 뒤 `after_commit`이 닫힐 때 나온다.
    """
    snapshot = capture(connection, story_id)
    after_commit.enter_context(coordinator.fence(story_id, snapshot))
