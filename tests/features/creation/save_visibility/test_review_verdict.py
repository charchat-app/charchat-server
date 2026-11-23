"""CREATE-US-08-AC04~AC08·AC15 내부 판정 계약(DB 불필요).

이미 분류된 신호만 받아 서버 규칙으로 결정하는지 본다. 신호 추출 정확도는 범위 밖이다.
고정값은 중립 합성값만 쓰고 원문을 담지 않는다.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.features.creation.save_visibility.rating_review import build_snapshot
from app.features.creation.save_visibility.review_verdict import (
    AGE_THRESHOLD,
    AgeContext,
    CharacterSignals,
    CommonSafety,
    CommonSafetyVerdict,
    Eligibility,
    EligibilityReason,
    EligibilitySignals,
    EligibilityVerdict,
    Kinship,
    MinorDepiction,
    Offering,
    decide_eligibility,
    decide_offering,
    eligibility_from_output,
    is_eligible,
    unavailable,
)

R = EligibilityReason


def snapshot(*ids):
    people = [{"id": i, "name": f"인물{i}", "description": "상세"} for i in ids or (1,)]
    return build_snapshot("세계", people, [])


def signals(character_id=1, **changes):
    base = {
        "character_id": character_id,
        "stated_age": None,
        "age_context": AgeContext.ADULT,
        "minor_depiction": MinorDepiction.ABSENT,
        "kinship_in_description": Kinship.NONE,
        "kinship_in_relation": Kinship.NONE,
    }
    return CharacterSignals(**{**base, **changes})


def decide(*characters, snap=None):
    characters = characters or (signals(),)
    snap = snap or snapshot(*(c.character_id for c in characters))
    return decide_eligibility(snap, EligibilitySignals(characters=characters))


def verdict(outcome, *reasons):
    return EligibilityVerdict(outcome=outcome, reasons=frozenset(reasons))


# --- 나이(AC04·AC05) ---------------------------------------------------------------------------


def test_age_threshold_is_twenty_inclusive_CREATE_US_08_AC04():
    assert AGE_THRESHOLD == 20
    assert decide(signals(stated_age=20)).outcome is Eligibility.ELIGIBLE
    assert decide(signals(stated_age=19)) == verdict(Eligibility.INELIGIBLE, R.UNDERAGE_STATED)


@pytest.mark.parametrize("context", list(AgeContext))
def test_stated_under_twenty_is_not_overridden_by_adult_context_CREATE_US_08_AC04(context):
    result = decide(signals(stated_age=17, age_context=context))
    assert result == verdict(Eligibility.INELIGIBLE, R.UNDERAGE_STATED)


@pytest.mark.parametrize("age", [None, 20, 35])
def test_child_depiction_is_not_overridden_by_age_field_CREATE_US_08_AC04(age):
    result = decide(signals(stated_age=age, minor_depiction=MinorDepiction.DEPICTED))
    assert result == verdict(Eligibility.INELIGIBLE, R.MINOR_DEPICTED)


def test_unstated_age_with_adult_context_qualifies_CREATE_US_08_AC05():
    assert decide(signals(stated_age=None, age_context=AgeContext.ADULT)) == verdict(
        Eligibility.ELIGIBLE
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"stated_age": None, "age_context": AgeContext.NONE},
        {"stated_age": None, "age_context": AgeContext.CONTRADICTORY},
        {"stated_age": 25, "age_context": AgeContext.CONTRADICTORY},
        {"stated_age": 25, "minor_depiction": MinorDepiction.UNCLEAR},
    ],
)
def test_ambiguous_age_is_undetermined_not_underage_CREATE_US_08_AC05(changes):
    result = decide(signals(**changes))
    assert result == verdict(Eligibility.UNDETERMINED, R.AGE_UNDETERMINED)
    assert not is_eligible(result)


def test_undetermined_underage_and_outage_are_distinct_CREATE_US_08_AC05():
    outcomes = {
        decide(signals(age_context=AgeContext.NONE)).outcome,
        decide(signals(stated_age=10)).outcome,
        unavailable().outcome,
    }
    assert outcomes == {Eligibility.UNDETERMINED, Eligibility.INELIGIBLE, Eligibility.UNAVAILABLE}


# --- 관계(AC06~AC08) ---------------------------------------------------------------------------


@pytest.mark.parametrize("kin", [Kinship.BIOLOGICAL_PARENT_CHILD, Kinship.BIOLOGICAL_SIBLING])
@pytest.mark.parametrize("field", ["kinship_in_description", "kinship_in_relation"])
def test_restricted_kinship_from_either_source_is_ineligible_CREATE_US_08_AC06(kin, field):
    result = decide(signals(**{field: kin}))
    assert result == verdict(Eligibility.INELIGIBLE, R.RESTRICTED_KINSHIP)


def test_description_kinship_is_not_cleared_by_blank_relation_CREATE_US_08_AC07():
    # 초기 관계를 비워도(관계 신호 NONE) 상세에서 확인된 제한 관계는 그대로 부적격이다.
    result = decide(
        signals(
            kinship_in_description=Kinship.BIOLOGICAL_SIBLING,
            kinship_in_relation=Kinship.NONE,
        )
    )
    assert result.outcome is Eligibility.INELIGIBLE


def test_blank_initial_relation_alone_is_not_rejection_CREATE_US_08_AC07():
    assert decide(signals(kinship_in_relation=Kinship.NONE)).outcome is Eligibility.ELIGIBLE


@pytest.mark.parametrize("field", ["kinship_in_description", "kinship_in_relation"])
def test_non_restricted_family_terms_are_not_disqualifying_CREATE_US_08_AC08(field):
    # 호칭·인척·무관한 가족은 분류기가 NON_RESTRICTED로 넘긴다.
    assert decide(signals(**{field: Kinship.NON_RESTRICTED})).outcome is Eligibility.ELIGIBLE


@pytest.mark.parametrize("field", ["kinship_in_description", "kinship_in_relation"])
def test_unclear_kinship_is_undetermined_CREATE_US_08_AC06(field):
    result = decide(signals(**{field: Kinship.UNCLEAR}))
    assert result == verdict(Eligibility.UNDETERMINED, R.KINSHIP_UNDETERMINED)


def test_confirmed_reason_dominates_undetermined_across_characters_CREATE_US_08_AC04():
    result = decide(
        signals(1, age_context=AgeContext.NONE),
        signals(2, kinship_in_relation=Kinship.BIOLOGICAL_PARENT_CHILD),
    )
    # 사유는 모두 남기고, 결과는 확인된 부적격이 판단 불가보다 앞선다.
    assert result == verdict(Eligibility.INELIGIBLE, R.RESTRICTED_KINSHIP, R.AGE_UNDETERMINED)


def test_every_character_must_qualify_CREATE_US_08_AC04():
    assert decide(signals(1), signals(2, stated_age=12)).outcome is Eligibility.INELIGIBLE
    assert decide(signals(1), signals(2)).outcome is Eligibility.ELIGIBLE


# --- 잘못된·누락 신호는 승인하지 않는다(AC04~AC08) -----------------------------------------------


@pytest.mark.parametrize(
    "characters, ids",
    [
        ((signals(1),), (1, 2)),  # 누락
        ((signals(1), signals(2)), (1,)),  # 모르는 인물
        ((signals(1), signals(1)), (1,)),  # 중복
        ((), (1,)),  # 빈 신호
    ],
)
def test_signals_must_cover_snapshot_characters_exactly_CREATE_US_08_AC05(characters, ids):
    result = decide_eligibility(snapshot(*ids), EligibilitySignals(characters=characters))
    assert result == verdict(Eligibility.UNAVAILABLE, R.INVALID_SIGNALS)


def test_empty_roster_cannot_approve_CREATE_US_08_AC05():
    # 인물이 없는 내용과 빈 신호가 "일치"해도 적격 근거가 없으므로 승인하지 않는다.
    empty = build_snapshot("세계", [], [])
    expected = verdict(Eligibility.UNAVAILABLE, R.INVALID_SIGNALS)
    assert decide_eligibility(empty, EligibilitySignals(characters=())) == expected
    assert eligibility_from_output(empty, '{"characters": []}') == expected


def _skipped(**nested):
    """검증을 건너뛴 신호: 정상 신호를 model_copy로 바꿔 바깥에 다시 끼운다."""
    outer = EligibilitySignals(characters=(signals(),))
    return outer.model_copy(update={"characters": (signals().model_copy(update=nested),)})


@pytest.mark.parametrize(
    "forged",
    [
        _skipped(minor_depiction="depicted"),  # 열거형이 아닌 문자열은 `is` 비교를 빠져나간다.
        _skipped(kinship_in_relation="biological_sibling"),
        _skipped(stated_age="12"),
        _skipped(minor_depiction=SimpleNamespace(value="absent")),
        _skipped(rationale="x"),  # 추가 필드
        EligibilitySignals(characters=(signals(),)).model_copy(update={"rationale": "x"}),
        EligibilitySignals.model_construct(characters=[signals()]),
        EligibilitySignals.model_construct(characters=({"character_id": 1},)),
        SimpleNamespace(characters=(signals(),)),
        None,
    ],
)
def test_public_decide_revalidates_skipped_signals_CREATE_US_08_AC04_AC05(forged):
    assert decide_eligibility(snapshot(1), forged) == verdict(
        Eligibility.UNAVAILABLE, R.INVALID_SIGNALS
    )


@pytest.mark.parametrize(
    "output",
    [
        None,
        True,
        "not json",
        b"{}",
        {"characters": [{"character_id": 1}]},  # 필드 누락
        # 닫힌 열거형 밖의 값
        '{"characters": [{"character_id": 1, "stated_age": null, "age_context": "maybe",'
        ' "minor_depiction": "absent", "kinship_in_description": "none",'
        ' "kinship_in_relation": "none"}]}',
        # 원문·설명 같은 추가 필드는 받지 않는다.
        '{"characters": [{"character_id": 1, "stated_age": null, "age_context": "adult",'
        ' "minor_depiction": "absent", "kinship_in_description": "none",'
        ' "kinship_in_relation": "none", "rationale": "x"}]}',
        # bool은 나이가 아니다.
        '{"characters": [{"character_id": 1, "stated_age": true, "age_context": "adult",'
        ' "minor_depiction": "absent", "kinship_in_description": "none",'
        ' "kinship_in_relation": "none"}]}',
        '{"characters": [{"character_id": 1, "stated_age": -1, "age_context": "adult",'
        ' "minor_depiction": "absent", "kinship_in_description": "none",'
        ' "kinship_in_relation": "none"}]}',
    ],
)
def test_invalid_output_cannot_approve_CREATE_US_08_AC05(output):
    result = eligibility_from_output(snapshot(1), output)
    assert result == verdict(Eligibility.UNAVAILABLE, R.INVALID_SIGNALS)


@pytest.mark.parametrize(
    "forged",
    [
        # 열거형 자리에 정상 값처럼 보이는 문자열을 끼워도 객체 경로는 엄격 검증으로 거부한다.
        _skipped(minor_depiction="absent"),
        _skipped(age_context="adult"),
        _skipped(kinship_in_relation="none"),
        EligibilitySignals(characters=(signals(),)).model_copy(update={"rationale": "x"}),
        EligibilitySignals.model_construct(characters=[signals()]),
    ],
)
def test_object_output_is_strictly_revalidated_CREATE_US_08_AC05(forged):
    result = eligibility_from_output(snapshot(1), forged)
    assert result == verdict(Eligibility.UNAVAILABLE, R.INVALID_SIGNALS)


def test_valid_json_output_is_decided_by_server_rules_CREATE_US_08_AC04():
    output = (
        '{"characters": [{"character_id": 1, "stated_age": 19, "age_context": "adult",'
        ' "minor_depiction": "absent", "kinship_in_description": "none",'
        ' "kinship_in_relation": "none"}]}'
    )
    assert eligibility_from_output(snapshot(1), output) == verdict(
        Eligibility.INELIGIBLE, R.UNDERAGE_STATED
    )
    assert eligibility_from_output(snapshot(1), EligibilitySignals(characters=(signals(),))) == (
        verdict(Eligibility.ELIGIBLE)
    )


@pytest.mark.parametrize(
    "outcome, reasons",
    [
        (Eligibility.ELIGIBLE, {R.UNDERAGE_STATED}),
        (Eligibility.INELIGIBLE, set()),
        (Eligibility.INELIGIBLE, {R.AGE_UNDETERMINED}),
        (Eligibility.UNDETERMINED, {R.UNDERAGE_STATED}),
        (Eligibility.UNAVAILABLE, {R.AGE_UNDETERMINED}),
        (Eligibility.UNAVAILABLE, {R.EVALUATOR_FAILURE, R.UNDERAGE_STATED}),
    ],
)
def test_verdict_rejects_inconsistent_outcome_and_reasons_CREATE_US_08_AC05(outcome, reasons):
    with pytest.raises(ValidationError):
        EligibilityVerdict(outcome=outcome, reasons=frozenset(reasons))


def test_is_eligible_rejects_unvalidated_or_foreign_values_CREATE_US_08_AC05():
    assert is_eligible(verdict(Eligibility.ELIGIBLE))
    forged = EligibilityVerdict.model_construct(
        outcome=Eligibility.ELIGIBLE, reasons=frozenset({R.UNDERAGE_STATED})
    )
    copied = [
        verdict(Eligibility.ELIGIBLE).model_copy(update=u)
        for u in ({"rationale": "x"}, {"outcome": "eligible"}, {"reasons": ()})
    ]
    for value in (forged, *copied, True, "eligible", None, Eligibility.ELIGIBLE, unavailable()):
        assert not is_eligible(value)


def test_verdict_and_signals_are_immutable_CREATE_US_08_AC05():
    result = verdict(Eligibility.ELIGIBLE)
    with pytest.raises(ValidationError):
        result.outcome = Eligibility.INELIGIBLE
    with pytest.raises(ValidationError):
        signals().stated_age = 30


# --- 제공 가능 여부 합성(AC15) -----------------------------------------------------------------

CLEAR = CommonSafetyVerdict(outcome=CommonSafety.CLEAR)
ELIGIBLE = verdict(Eligibility.ELIGIBLE)
NON_APPROVING = [
    None,
    verdict(Eligibility.INELIGIBLE, R.UNDERAGE_STATED),
    verdict(Eligibility.UNDETERMINED, R.AGE_UNDETERMINED),
    unavailable(),
]


@pytest.mark.parametrize("rating", ["SAFE", "UNSAFE"])
@pytest.mark.parametrize("eligibility", [ELIGIBLE, *NON_APPROVING])
def test_common_block_overrides_both_ratings_CREATE_US_08_AC15(rating, eligibility):
    blocked = CommonSafetyVerdict(outcome=CommonSafety.BLOCKED)
    assert decide_offering(rating, blocked, eligibility) is Offering.BLOCKED


@pytest.mark.parametrize("rating", ["SAFE", "UNSAFE"])
@pytest.mark.parametrize(
    "common",
    [
        CommonSafetyVerdict(outcome=CommonSafety.UNCHECKED),
        CommonSafetyVerdict(outcome=CommonSafety.UNAVAILABLE),
        None,
        True,
        "clear",
        CommonSafety.CLEAR,
        CommonSafetyVerdict.model_construct(outcome="clear"),
        # 진짜 CLEAR 열거형이어도 model_copy로 금지된 추가 필드가 붙으면 통과가 아니다.
        CommonSafetyVerdict(outcome=CommonSafety.CLEAR).model_copy(update={"rationale": "x"}),
        CommonSafetyVerdict(outcome=CommonSafety.CLEAR).model_copy(update={"outcome": "clear"}),
    ],
)
def test_unchecked_or_outage_common_is_not_clearance_CREATE_US_08_AC15(rating, common):
    assert decide_offering(rating, common, ELIGIBLE) is Offering.NOT_CLEARED


@pytest.mark.parametrize("eligibility", [ELIGIBLE, *NON_APPROVING])
def test_safe_with_clear_common_needs_no_eligibility_CREATE_US_08_AC15(eligibility):
    assert decide_offering("SAFE", CLEAR, eligibility) is Offering.OFFERABLE


def test_unsafe_requires_clear_common_and_eligibility_CREATE_US_08_AC15():
    assert decide_offering("UNSAFE", CLEAR, ELIGIBLE) is Offering.OFFERABLE


@pytest.mark.parametrize("eligibility", [*NON_APPROVING, True, "eligible"])
def test_unsafe_without_eligibility_is_not_cleared_CREATE_US_08_AC15(eligibility):
    assert decide_offering("UNSAFE", CLEAR, eligibility) is Offering.NOT_CLEARED


@pytest.mark.parametrize("rating", [None, "safe", "", "OTHER"])
def test_unknown_rating_is_not_cleared_CREATE_US_08_AC15(rating):
    assert decide_offering(rating, CLEAR, ELIGIBLE) is Offering.NOT_CLEARED
