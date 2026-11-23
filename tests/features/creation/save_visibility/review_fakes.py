"""CREATE-US-08-AC02·AC04~AC08·AC14 테스트 전용 가짜 평가기와 통과 기록(프로세스 메모리, 비영속)."""

import threading

from app.features.creation.save_visibility.rating_review import ProcessLastPassStore
from app.features.creation.save_visibility.review_verdict import (
    Eligibility,
    EligibilityReason,
    EligibilityVerdict,
    unavailable,
)

ELIGIBLE = EligibilityVerdict(outcome=Eligibility.ELIGIBLE, reasons=frozenset())
INELIGIBLE = EligibilityVerdict(
    outcome=Eligibility.INELIGIBLE, reasons=frozenset({EligibilityReason.UNDERAGE_STATED})
)
UNDETERMINED = EligibilityVerdict(
    outcome=Eligibility.UNDETERMINED, reasons=frozenset({EligibilityReason.AGE_UNDETERMINED})
)
UNAVAILABLE = unavailable()


class MemoryLastPassStore(ProcessLastPassStore):
    """프로세스 메모리 통과 기록에 기록 순서(`history`)를 더한다. `fail_get`·`fail_set`은 예외."""

    def __init__(self):
        super().__init__()
        self.history = []
        self.fail_get = None
        self.fail_set = None

    def get(self, story_id):
        if self.fail_get is not None:
            raise self.fail_get
        return super().get(story_id)

    def set(self, story_id, snapshot):
        if self.fail_set is not None:
            raise self.fail_set
        super().set(story_id, snapshot)
        self.history.append((story_id, snapshot))


class FakeEvaluator:
    """`gate`가 열릴 때까지 막힌다.

    `verdict`는 그대로 돌려줄 값(판정·잘못된 값), 던질 예외 객체, 또는 snapshot을 받는 함수다.
    `started`는 평가를 시작하면, `finished`는 판정을 돌려주기 직전에 켜진다.
    """

    def __init__(self, verdict=ELIGIBLE):
        self.verdict = verdict
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        self.on_evaluate = None

    def evaluate(self, story_id, snapshot):
        self.calls.append((story_id, snapshot))
        if self.on_evaluate is not None:
            self.on_evaluate(story_id, snapshot)
        self.started.set()
        assert self.gate.wait(10)
        verdict = self.verdict(snapshot) if callable(self.verdict) else self.verdict
        self.finished.set()
        if isinstance(verdict, Exception):
            raise verdict
        return verdict
