"""CREATE-US-08-AC09 검증된 사유 대상(인물·관계) 계약(DB·네트워크 불필요).

판정 규칙(적격 여부)은 바꾸지 않는다. 대상은 인물별 검증 신호에서만 만들고, 판정 사유·검토 비교
내용에 묶이지 않는 대상은 뺀다. 고정값은 중립 합성값만 쓴다.
"""

import pytest

from app.features.creation.save_visibility.jev_decisions import JevDecisionsEvaluator
from app.features.creation.save_visibility.rating_review import (
    FAILURE,
    RatingReviewCoordinator,
    review_result,
    target_label,
)
from app.features.creation.save_visibility.review_verdict import (
    AgeContext,
    AttributedVerdict,
    CharacterSignals,
    Eligibility,
    EligibilityReason,
    EligibilitySignals,
    EligibilityVerdict,
    Kinship,
    MinorDepiction,
    ReviewTarget,
    TargetKind,
    decide_attributed,
    decide_eligibility,
)
from tests.features.creation.save_visibility.review_fakes import ELIGIBLE, INELIGIBLE
from tests.features.creation.save_visibility.test_jev_decisions import FakeTransport, snap

R = EligibilityReason
CHARACTER, RELATIONSHIP = TargetKind.CHARACTER, TargetKind.RELATIONSHIP


def person(character_id, **overrides):
    values = {
        "character_id": character_id,
        "stated_age": 32,
        "age_context": AgeContext.ADULT,
        "minor_depiction": MinorDepiction.ABSENT,
        "kinship_in_description": Kinship.NONE,
        "kinship_in_relation": Kinship.NONE,
        **overrides,
    }
    return CharacterSignals(**values)


def signals(*people):
    return EligibilitySignals(characters=tuple(people))


def target(reason, kind, character_id):
    return ReviewTarget(reason=reason, kind=kind, character_id=character_id)


# --- 판정 계약: 인물별 신호 → 대상 -----------------------------------------------------------


def test_two_distinct_underage_characters_each_get_a_target_CREATE_US_08_AC09():
    snapshot = snap((9001, 9002))
    given = signals(person(9001, stated_age=17), person(9002, stated_age=16))
    attributed = decide_attributed(snapshot, given)
    assert attributed.verdict == decide_eligibility(snapshot, given)  # 판정은 그대로
    assert attributed.targets == {
        target(R.UNDERAGE_STATED, CHARACTER, 9001),
        target(R.UNDERAGE_STATED, CHARACTER, 9002),
    }


def test_only_the_flagged_character_is_targeted_CREATE_US_08_AC09():
    snapshot = snap((9001, 9002))
    given = signals(person(9001), person(9002, minor_depiction=MinorDepiction.DEPICTED))
    assert decide_attributed(snapshot, given).targets == {target(R.MINOR_DEPICTED, CHARACTER, 9002)}


@pytest.mark.parametrize("field", ["kinship_in_description", "kinship_in_relation"])
def test_restricted_kinship_targets_user_character_pair_CREATE_US_08_AC09(field):
    snapshot = snap((9001, 9002))
    given = signals(person(9001), person(9002, **{field: Kinship.BIOLOGICAL_SIBLING}))
    attributed = decide_attributed(snapshot, given)
    assert attributed.verdict.outcome is Eligibility.INELIGIBLE
    # 제한 혈연 신호는 계약상 늘 「유저 ↔ 이 명단 인물」이다(출발·방향은 추측하지 않는다).
    assert attributed.targets == {target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9002)}


def test_undetermined_reasons_get_matching_targets_CREATE_US_08_AC09():
    snapshot = snap((9001,))
    given = signals(
        person(
            9001, stated_age=None, age_context=AgeContext.NONE, kinship_in_relation=Kinship.UNCLEAR
        )
    )
    attributed = decide_attributed(snapshot, given)
    assert attributed.verdict.outcome is Eligibility.UNDETERMINED
    assert attributed.targets == {
        target(R.AGE_UNDETERMINED, CHARACTER, 9001),
        target(R.KINSHIP_UNDETERMINED, RELATIONSHIP, 9001),
    }


def test_eligible_and_invalid_signals_have_no_targets_CREATE_US_08_AC09():
    snapshot = snap((9001,))
    assert decide_attributed(snapshot, signals(person(9001))).targets == frozenset()
    # 명단과 맞지 않는 신호는 판정 불가(실패)이며 대상도 없다.
    mismatch = decide_attributed(snapshot, signals(person(9999, stated_age=17)))
    assert mismatch.verdict.outcome is Eligibility.UNAVAILABLE
    assert mismatch.targets == frozenset()


@pytest.mark.parametrize(
    ("reason", "kind", "character_id"),
    [
        (R.RESTRICTED_KINSHIP, CHARACTER, 1),  # 혈연은 관계 대상이다
        (R.UNDERAGE_STATED, RELATIONSHIP, 1),  # 나이는 인물 대상이다
        (R.RESTRICTED_KINSHIP, RELATIONSHIP, None),  # 관계는 명단 인물이 있어야 한다
        (R.INVALID_SIGNALS, CHARACTER, 1),  # 장애 사유는 대상이 없다
    ],
)
def test_target_contract_rejects_inconsistent_shapes_CREATE_US_08_AC09(reason, kind, character_id):
    with pytest.raises(ValueError):
        ReviewTarget(reason=reason, kind=kind, character_id=character_id)


# --- 결과 변환: 사유·검토 비교 내용에 묶이지 않는 대상은 뺀다 ---------------------------------


def attributed(verdict, *targets):
    return AttributedVerdict(verdict=verdict, targets=frozenset(targets))


def test_review_result_keeps_only_targets_tied_to_reason_and_snapshot_CREATE_US_08_AC09():
    snapshot = snap((9001, 9002))
    value = attributed(
        INELIGIBLE,  # 사유: underage_stated
        target(R.UNDERAGE_STATED, CHARACTER, 9001),
        target(R.UNDERAGE_STATED, CHARACTER, None),  # 유저 캐릭터
        target(R.MINOR_DEPICTED, CHARACTER, 9002),  # 판정 사유에 없다
        target(R.UNDERAGE_STATED, CHARACTER, 4242),  # 검토 비교 내용에 없는 인물
    )
    result = review_result(value, snapshot)
    assert (result.effective_rating, result.status, result.reason) == (
        "SAFE",
        "completed",
        "ineligible:underage_stated",
    )
    assert result.targets == {
        target(R.UNDERAGE_STATED, CHARACTER, 9001),
        target(R.UNDERAGE_STATED, CHARACTER, None),
    }


def test_review_result_policy_is_unchanged_by_targets_CREATE_US_08_AC09():
    snapshot = snap((9001,))
    plain = review_result(INELIGIBLE)
    with_targets = review_result(
        attributed(INELIGIBLE, target(R.UNDERAGE_STATED, CHARACTER, 9001)), snapshot
    )
    assert (plain.effective_rating, plain.status, plain.reason, plain.passed) == (
        with_targets.effective_rating,
        with_targets.status,
        with_targets.reason,
        with_targets.passed,
    )
    assert plain.targets == frozenset()  # 대상 없는 기존 판정(예전 평가기)
    # 적격은 대상을 붙여도 대상이 없다.
    assert review_result(attributed(ELIGIBLE), snapshot).targets == frozenset()
    assert review_result(attributed(ELIGIBLE), snapshot).passed is True
    # 대상만 있고 스냅숏이 없으면 묶을 수 없으므로 뺀다.
    assert (
        review_result(attributed(INELIGIBLE, target(R.UNDERAGE_STATED, CHARACTER, 9001))).targets
        == frozenset()
    )
    assert FAILURE.targets == frozenset()


VALID_TARGET = ReviewTarget(reason=R.UNDERAGE_STATED, kind=CHARACTER, character_id=9001)


@pytest.mark.parametrize(
    ("targets", "kept"),
    [
        (frozenset({"9001"}), frozenset()),
        ("9001", frozenset()),
        (None, frozenset()),
        (42, frozenset()),
        ({"x": VALID_TARGET}, frozenset()),
        (
            # 튜플: 문자열 사유 위조본이 열거형 대상과 같은 값으로 합쳐지지 않게 한다.
            (
                ReviewTarget.model_construct(
                    reason="underage_stated", kind=CHARACTER, character_id=9001
                ),
                ReviewTarget.model_construct(
                    reason=R.RESTRICTED_KINSHIP, kind=CHARACTER, character_id=9001
                ),
                {"reason": "underage_stated", "kind": "character", "character_id": 9001},
                VALID_TARGET,
            ),
            frozenset({VALID_TARGET}),
        ),
    ],
    ids=["str-item", "str", "none", "int", "dict", "mixed"],
)
def test_malformed_targets_keep_valid_verdict_and_downgrade_CREATE_US_08_AC09(targets, kept):
    """대상 메타데이터가 잘못돼도 독립적으로 유효한 판정(강등)은 그대로이고 잘못된 대상만 뺀다."""
    forged = AttributedVerdict.model_construct(verdict=INELIGIBLE, targets=targets)
    result = review_result(forged, snap((9001,)))
    assert (result.effective_rating, result.status, result.reason, result.passed) == (
        "SAFE",
        "completed",
        "ineligible:underage_stated",
        False,
    )
    assert result.targets == kept


def test_wrapper_extra_field_keeps_verdict_and_drops_targets_CREATE_US_08_AC09():
    forged = AttributedVerdict.model_construct(
        verdict=INELIGIBLE, targets=frozenset({VALID_TARGET})
    )
    forged.__dict__["raw"] = "평가기 원문"  # 검증을 건너뛴 계약 밖 필드
    result = review_result(forged, snap((9001,)))
    assert (result.effective_rating, result.status, result.reason) == (
        "SAFE",
        "completed",
        "ineligible:underage_stated",
    )
    assert result.targets == frozenset()


@pytest.mark.parametrize(
    "bad_verdict",
    [
        EligibilityVerdict.model_construct(outcome="eligible", reasons=frozenset()),
        "eligible",
        None,
    ],
    ids=["unvalidated", "str", "none"],
)
def test_malformed_verdict_in_wrapper_still_fails_CREATE_US_08_AC09(bad_verdict):
    forged = AttributedVerdict.model_construct(verdict=bad_verdict, targets=frozenset())
    result = review_result(forged, snap((9001,)))
    assert (result.status, result.reason, result.passed, result.targets) == (
        "failed",
        "unavailable:invalid_signals",
        False,
        frozenset(),
    )


def test_coordinator_keeps_downgrade_when_targets_malformed_CREATE_US_08_AC09():
    recorded = []

    class Results:
        def apply(self, story_id, snapshot, ticket, result, namespace=None):
            recorded.append(result)
            return True

    class Malformed:
        def evaluate_attributed(self, story_id, snapshot):
            return AttributedVerdict.model_construct(verdict=INELIGIBLE, targets="9001")

    coordinator = RatingReviewCoordinator(Malformed(), results=Results())
    try:
        from datetime import UTC, datetime

        with coordinator.fence(1, snap((9001,)), datetime.now(UTC)):
            pass
        assert coordinator.wait(5)
    finally:
        coordinator.close()
    assert [(r.effective_rating, r.status, r.reason, r.targets) for r in recorded] == [
        ("SAFE", "completed", "ineligible:underage_stated", frozenset())
    ]


# --- 표시 라벨: 고정 역할 문구와 겹치는 명단 이름은 모호하므로 뺀다 ------------------------------


@pytest.mark.parametrize("roster_name", ["유저", " 유저 "])
def test_roster_named_user_label_is_omitted_CREATE_US_08_AC09(roster_name):
    names = {9001: roster_name, 9002: "도윤"}
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9001), names) is None
    assert target_label(target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9001), names) is None
    # 다른 인물·유저 캐릭터(고정 역할)는 그대로다.
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9002), names) == "도윤"
    assert target_label(target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9002), names) == "유저 ↔ 도윤"
    assert target_label(target(R.MINOR_DEPICTED, CHARACTER, None), names) == "유저"


@pytest.mark.parametrize(
    ("roster_name", "label"),
    [
        ("도\t윤", "도 윤"),
        ("도\n윤", "도 윤"),
        ("도\r\n윤", "도 윤"),
        ("\t도윤\n", "도윤"),
        ("도 \t\r\n 윤", "도 윤"),
    ],
)
def test_roster_name_line_breaks_become_single_line_label_CREATE_US_08_AC09(roster_name, label):
    # 앱 readTarget은 제어 문자가 든 라벨을 거부하므로 탭·LF·CR은 한 줄 공백으로 바꿔 낸다.
    names = {9001: roster_name}
    pair = target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9001)
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9001), names) == label
    assert target_label(pair, names) == f"유저 ↔ {label}"
    assert names == {9001: roster_name}  # 명단 원본 이름은 바꾸지 않는다.


@pytest.mark.parametrize(
    "roster_name",
    ["도\x00윤", "도\x1b윤", "도\x7f윤", "도\x85윤", "도 윤", "도‮윤", "\t\n\r", ""],
)
def test_roster_name_with_other_controls_is_omitted_CREATE_US_08_AC09(roster_name):
    names = {9001: roster_name, 9002: "도윤"}
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9001), names) is None
    assert target_label(target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9001), names) is None
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9002), names) == "도윤"


@pytest.mark.parametrize(
    ("first", "second"),
    [("도\t윤", "도 윤"), ("도\n윤", "도\r윤"), ("도윤", "\t도윤")],
)
def test_roster_names_colliding_after_normalization_are_omitted_CREATE_US_08_AC09(first, second):
    # 정규화 뒤 같은 라벨이 되는 두 인물은 구별되지 않으므로 둘 다 뺀다.
    names = {9001: first, 9002: second, 9003: "서아"}
    for cid in (9001, 9002):
        assert target_label(target(R.UNDERAGE_STATED, CHARACTER, cid), names) is None
        assert target_label(target(R.RESTRICTED_KINSHIP, RELATIONSHIP, cid), names) is None
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9003), names) == "서아"


@pytest.mark.parametrize("roster_name", ["도윤﻿", "﻿도윤", "도﻿윤", "﻿"])
def test_roster_name_with_feff_is_omitted_CREATE_US_08_AC09(roster_name):
    # 앱 JS trim은 FEFF를 지우고 Python strip은 남긴다. 앞뒤 FEFF 이름이 「도윤」과 겹쳐도
    # FEFF 쪽만 빠지고 다른 유효 인물은 그대로 남는다.
    names = {9001: roster_name, 9002: "도윤", 9003: "서아"}
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9001), names) is None
    assert target_label(target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9001), names) is None
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9002), names) == "도윤"
    assert target_label(target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9002), names) == "유저 ↔ 도윤"
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9003), names) == "서아"
    assert names[9001] == roster_name  # 명단 원본 이름은 바꾸지 않는다.


@pytest.mark.parametrize("roster_name", ["\n유저", "유저\r\n", "\t유저\t"])
def test_roster_name_normalizing_to_user_is_omitted_CREATE_US_08_AC09(roster_name):
    names = {9001: roster_name, 9002: "도윤"}
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9001), names) is None
    assert target_label(target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9001), names) is None
    assert target_label(target(R.MINOR_DEPICTED, CHARACTER, None), names) == "유저"
    assert target_label(target(R.UNDERAGE_STATED, CHARACTER, 9002), names) == "도윤"


# --- Jev 어댑터: 같은 응답에서 판정과 대상을 함께 만든다 ------------------------------------------


def test_jev_attributed_matches_plain_verdict_and_targets_CREATE_US_08_AC09():
    snapshot = snap((9001, 9002), relations=(("user", None, "character", 9002, "남매"),))
    overrides = {
        "c1_age_band": "under_20",
        "c2_kinship_in_relation": "biological_sibling",
        "user_character_age": "minor",
    }
    evaluator = JevDecisionsEvaluator(FakeTransport(**overrides))
    result = evaluator.evaluate_attributed(77, snapshot)
    assert result.verdict == JevDecisionsEvaluator(FakeTransport(**overrides)).evaluate(
        77, snapshot
    )
    assert result.verdict == EligibilityVerdict(
        outcome=Eligibility.INELIGIBLE,
        reasons=frozenset({R.UNDERAGE_STATED, R.RESTRICTED_KINSHIP, R.MINOR_DEPICTED}),
    )
    assert result.targets == {
        target(R.UNDERAGE_STATED, CHARACTER, 9001),
        target(R.RESTRICTED_KINSHIP, RELATIONSHIP, 9002),
        target(R.MINOR_DEPICTED, CHARACTER, None),  # 유저 캐릭터(고정 역할)
    }


def test_jev_failure_has_no_targets_CREATE_US_08_AC09():
    evaluator = JevDecisionsEvaluator(FakeTransport(lambda request: RuntimeError("down")))
    result = evaluator.evaluate_attributed(77, snap())
    assert result.verdict.outcome is Eligibility.UNAVAILABLE
    assert result.targets == frozenset()


def test_coordinator_prefers_attributed_evaluation_CREATE_US_08_AC09():
    snapshot = snap((9001,))
    recorded = []

    class Results:
        def apply(self, story_id, snapshot, ticket, result, namespace=None):
            recorded.append(result)
            return True

    class Attributing:
        def evaluate(self, story_id, snapshot):  # pragma: no cover - 쓰이면 안 된다
            raise AssertionError("evaluate_attributed가 있으면 그것을 쓴다")

        def evaluate_attributed(self, story_id, snapshot):
            return attributed(INELIGIBLE, target(R.UNDERAGE_STATED, CHARACTER, 9001))

    coordinator = RatingReviewCoordinator(Attributing(), results=Results())
    try:
        from datetime import UTC, datetime

        with coordinator.fence(1, snapshot, datetime.now(UTC)):
            pass
        assert coordinator.wait(5)
    finally:
        coordinator.close()
    assert [r.targets for r in recorded] == [{target(R.UNDERAGE_STATED, CHARACTER, 9001)}]
