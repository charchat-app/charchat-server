"""CREATE-US-08-AC02·AC14 검토 비교 내용과 조정기(DB 불필요)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from app.features.creation.save_visibility.rating_review import (
    RatingReviewCoordinator,
    build_snapshot,
)
from app.features.creation.save_visibility.review_verdict import (
    Eligibility,
    EligibilityReason,
    EligibilityVerdict,
)
from tests.features.creation.save_visibility.review_fakes import (
    ELIGIBLE,
    INELIGIBLE,
    UNAVAILABLE,
    UNDETERMINED,
    FakeEvaluator,
    MemoryLastPassStore,
)

STORY = 7


def snap(world="세계", detail="상세", relations=(), name="인물"):
    return build_snapshot(world, [{"id": 1, "name": name, "description": detail}], list(relations))


def relation(label, target_kind="user", target=None, source=1):
    return {
        "source_kind": "character",
        "source_character_id": source,
        "target_kind": target_kind,
        "target_character_id": target,
        "label": label,
    }


@pytest.fixture
def parts():
    evaluator, store = FakeEvaluator(), MemoryLastPassStore()
    coordinator = RatingReviewCoordinator(evaluator, store)
    yield evaluator, store, coordinator
    evaluator.gate.set()
    coordinator.close()


# --- 비교 내용(AC14) -------------------------------------------------------------------------


def test_snapshot_is_order_independent_and_skips_blank_relations_CREATE_US_08_AC14():
    people = [
        {"id": 2, "name": "둘", "description": "둘"},
        {"id": 1, "name": "하나", "description": "하나"},
    ]
    links = [relation("친구"), relation(None, "character", 2), relation("   ", "character", 2)]
    first = build_snapshot("세계", people, links)
    again = build_snapshot("세계", list(reversed(people)), [relation("친구")])
    assert first == again
    # 인물·관계 client_key는 비교하지 않는다.
    renamed = [{**p, "client_key": "x"} for p in people]
    assert build_snapshot("세계", renamed, [{**relation("친구"), "client_key": "k"}]) == first


@pytest.mark.parametrize(
    "changed",
    [
        snap(world="다른 세계"),
        snap(detail="다른 상세"),
        snap(name="새 이름"),
        snap(relations=[relation("연인")]),
        # 같은 상세라도 인물 정체(id)가 다르면 다르다.
        build_snapshot("세계", [{"id": 2, "name": "인물", "description": "상세"}], []),
        # 같은 라벨이라도 관계 방향·대상이 다르면 다르다.
        snap(relations=[relation("친구", "character", 2)]),
    ],
)
def test_snapshot_detects_reviewed_changes_CREATE_US_08_AC14(changed):
    base = snap(relations=[relation("친구")]) if changed.relations else snap()
    assert changed != base


def test_snapshot_repr_has_no_story_content_CREATE_US_08_AC14():
    text = repr(snap(world="비밀 세계관", detail="비밀 상세", relations=[relation("비밀 관계")]))
    assert "비밀" not in text


# --- 조정기(AC02·AC14) -----------------------------------------------------------------------


def test_pass_is_recorded_and_same_snapshot_is_not_reviewed_again_CREATE_US_08_AC14(parts):
    evaluator, store, coordinator = parts
    assert coordinator.publish(STORY, snap()) == "submitted"
    assert coordinator.wait(5)
    assert store.get(STORY) == snap()
    assert coordinator.publish(STORY, snap()) == "already_passed"
    assert len(evaluator.calls) == 1


def test_same_active_snapshot_is_deduplicated_CREATE_US_08_AC14(parts):
    evaluator, store, coordinator = parts
    evaluator.gate.clear()
    assert coordinator.publish(STORY, snap()) == "submitted"
    assert evaluator.started.wait(5)
    assert coordinator.publish(STORY, snap()) == "deduplicated"
    evaluator.gate.set()
    assert coordinator.wait(5)
    assert len(evaluator.calls) == 1
    assert store.history == [(STORY, snap())]


def test_superseded_completion_is_discarded_CREATE_US_08_AC14(parts):
    evaluator, store, coordinator = parts
    evaluator.gate.clear()
    coordinator.publish(STORY, snap())
    assert evaluator.started.wait(5)
    newer = snap(world="새 세계")
    coordinator.publish(STORY, newer)
    evaluator.gate.set()
    assert coordinator.wait(5)
    # 앞선 내용이 통과해도 이미 새 내용이 저장됐으므로 마지막 통과가 되지 않는다.
    assert store.history == [(STORY, newer)]


def test_out_of_order_handoff_of_older_generation_is_ignored_CREATE_US_08_AC02(parts):
    evaluator, store, coordinator = parts
    with coordinator.fence(STORY, snap()) as older:
        assert coordinator.publish(STORY, snap(world="새 세계")) == "submitted"
    assert older.outcome == "stale"
    assert coordinator.wait(5)
    assert [s for _, s in evaluator.calls] == [snap(world="새 세계")]


@pytest.mark.parametrize("commit", [True, False])
def test_inflight_pass_waits_for_superseding_commit_or_rollback_CREATE_US_08_AC14(parts, commit):
    evaluator, store, coordinator = parts
    evaluator.gate.clear()
    coordinator.publish(STORY, snap())
    assert evaluator.started.wait(5)

    def finalize_save():
        with coordinator.fence(STORY, None):
            evaluator.gate.set()
            assert not coordinator.wait(0.1)
            assert store.get(STORY) is None
            if not commit:
                raise ValueError("커밋 실패")

    if commit:
        finalize_save()
    else:
        with pytest.raises(ValueError, match="커밋 실패"):
            finalize_save()
    assert coordinator.wait(5)
    assert store.get(STORY) == (None if commit else snap())


def test_revert_to_passed_snapshot_supersedes_in_flight_review_CREATE_US_08_AC14(parts):
    evaluator, store, coordinator = parts
    coordinator.publish(STORY, snap())
    assert coordinator.wait(5)
    evaluator.gate.clear()
    evaluator.started.clear()
    coordinator.publish(STORY, snap(world="고침"))
    assert evaluator.started.wait(5)
    assert coordinator.publish(STORY, snap()) == "already_passed"
    evaluator.gate.set()
    assert coordinator.wait(5)
    assert store.get(STORY) == snap()
    assert store.history == [(STORY, snap())]


NOT_APPROVING = [
    INELIGIBLE,
    UNDETERMINED,
    UNAVAILABLE,
    RuntimeError("공급자 오류"),
    # 타입이 맞지 않는 값·검증을 건너뛴 판정은 통과가 아니다(예전 bool 계약 포함).
    True,
    False,
    None,
    "eligible",
    Eligibility.ELIGIBLE,
    EligibilityVerdict.model_construct(
        outcome=Eligibility.ELIGIBLE, reasons=frozenset({EligibilityReason.UNDERAGE_STATED})
    ),
]


@pytest.mark.parametrize("verdict", NOT_APPROVING)
def test_failed_review_keeps_older_pass_and_is_not_reused_CREATE_US_08_AC05_AC14(parts, verdict):
    evaluator, store, coordinator = parts
    coordinator.publish(STORY, snap())
    assert coordinator.wait(5)
    changed = snap(world="고침")
    evaluator.verdict = verdict
    assert coordinator.publish(STORY, changed) == "submitted"
    assert coordinator.wait(5)
    assert store.get(STORY) == snap()
    # 실패한 내용은 통과로 남지 않아, 같은 내용을 다시 저장하면 다시 검토한다.
    evaluator.verdict = ELIGIBLE
    assert coordinator.publish(STORY, changed) == "submitted"
    assert coordinator.wait(5)
    assert store.get(STORY) == changed
    assert len(evaluator.calls) == 3


@pytest.mark.parametrize("verdict", [INELIGIBLE, UNDETERMINED, UNAVAILABLE, True])
def test_non_approval_logs_no_story_content_CREATE_US_08_AC05(parts, caplog, verdict):
    evaluator, store, coordinator = parts
    evaluator.verdict = verdict
    caplog.set_level("DEBUG", logger="app.features.creation.save_visibility.rating_review")
    coordinator.publish(STORY, snap(world="내용표식", detail="내용표식"))
    assert coordinator.wait(5)
    assert store.get(STORY) is None
    assert caplog.records
    assert all("내용표식" not in r.getMessage() for r in caplog.records)


LEAK = "원문표식"


class Opaque:
    """`.value`에 원문을 담은 해시 가능한 가짜 값."""

    value = LEAK


@pytest.mark.parametrize(
    "verdict",
    [
        # 검증을 건너뛴 판정 객체의 필드에 원문이 실린 경우
        INELIGIBLE.model_copy(update={"reasons": frozenset({Opaque()})}),
        INELIGIBLE.model_copy(update={"outcome": SimpleNamespace(value=LEAK)}),
        EligibilityVerdict.model_construct(outcome=SimpleNamespace(value=LEAK), reasons=()),
        INELIGIBLE.model_copy(update={"rationale": LEAK}),
        # 판정이 아닌 객체(타입 이름 포함)
        SimpleNamespace(outcome=SimpleNamespace(value=LEAK), reasons=[LEAK]),
        type(LEAK, (), {})(),
    ],
)
def test_malformed_verdict_logs_only_fixed_code_CREATE_US_08_AC05(parts, caplog, verdict):
    evaluator, store, coordinator = parts
    evaluator.verdict = verdict
    caplog.set_level("DEBUG", logger="app.features.creation.save_visibility.rating_review")
    coordinator.publish(STORY, snap())
    assert coordinator.wait(5)
    assert store.get(STORY) is None
    assert caplog.records
    assert all(LEAK not in r.getMessage() for r in caplog.records)


def test_stories_are_independent_CREATE_US_08_AC14(parts):
    evaluator, store, coordinator = parts
    coordinator.publish(STORY, snap())
    coordinator.publish(STORY + 1, snap())
    assert coordinator.wait(5)
    assert store.get(STORY) == store.get(STORY + 1) == snap()
    assert len(evaluator.calls) == 2


def test_wait_observes_cancelled_queued_work_CREATE_US_08_AC02(parts, monkeypatch):
    evaluator, _, coordinator = parts
    evaluator.gate.clear()
    coordinator.publish(STORY, snap())
    assert evaluator.started.wait(5)
    coordinator.publish(STORY + 1, snap())
    waiting = Event()
    original_wait = coordinator._cond.wait

    def mark_wait(timeout=None):
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(coordinator._cond, "wait", mark_wait)
    with ThreadPoolExecutor(1) as pool:
        completed = pool.submit(coordinator.wait, 2)
        try:
            assert waiting.wait(1)
            coordinator.close()
        finally:
            evaluator.gate.set()
        assert completed.result(timeout=3)
    assert len(evaluator.calls) == 1


class RecordingResults:
    """결과 보관소 가짜(DB 없음). 요청 표마다 받은 결과만 기록한다."""

    def __init__(self):
        self.applied = []

    def apply(self, story_id, snapshot, ticket, result, namespace=None):
        self.applied.append((story_id, ticket, result.status))
        return True


def test_persisted_queue_evaluates_only_exact_latest_generation_CREATE_US_08_AC14():
    """결과 저장 경로: 대기 B → 재사용 A → 다시 B면 최신 세대 B만 평가한다."""
    evaluator, results = FakeEvaluator(), RecordingResults()
    coordinator = RatingReviewCoordinator(evaluator, results=results, reuse_namespace="fake.v1")
    try:
        evaluator.gate.clear()
        # 작업자 1개를 다른 스토리 검토로 막아 B를 대기열에 둔다.
        with coordinator.fence(STORY + 1, snap(world="막기"), "t0"):
            pass
        assert evaluator.started.wait(5)
        with coordinator.fence(STORY, snap(world="B"), "t1") as first_b:
            pass
        with coordinator.fence(STORY, snap(), "t2", reused=True) as reused_a:
            pass
        with coordinator.fence(STORY, snap(world="B"), "t3") as latest_b:
            pass
        assert (first_b.outcome, reused_a.outcome, latest_b.outcome) == (
            "submitted",
            "reused",
            "submitted",
        )
        evaluator.gate.set()
        assert coordinator.wait(5)
        assert [(s, x) for s, x in evaluator.calls if s == STORY] == [(STORY, snap(world="B"))]
        # 앞선 세대(t1)는 평가하지 않고 실패 기록만 시도한다(요청 표가 달라 DB에는 쓰이지 않는다).
        # 최신 세대(t3)만 평가 결과를 쓴다.
        assert (STORY, "t1", "failed") in results.applied
        assert (STORY, "t1", "completed") not in results.applied
        assert (STORY, "t3", "completed") in results.applied
    finally:
        evaluator.gate.set()
        coordinator.close()


def test_legacy_queue_keeps_content_dedupe_CREATE_US_08_AC14(parts):
    """프로세스 경로(결과 보관소 없음)는 기존처럼 같은 내용의 대기 검토를 중복 제거한다."""
    evaluator, store, coordinator = parts
    evaluator.gate.clear()
    coordinator.publish(STORY + 1, snap(world="막기"))
    assert evaluator.started.wait(5)
    assert coordinator.publish(STORY, snap(world="B")) == "submitted"
    assert coordinator.publish(STORY, None) == "invalidated"
    assert coordinator.publish(STORY, snap(world="B")) == "deduplicated"
    evaluator.gate.set()
    assert coordinator.wait(5)
    assert [x for s, x in evaluator.calls if s == STORY] == [snap(world="B")]
    assert store.get(STORY) == snap(world="B")
