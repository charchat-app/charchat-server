"""CREATE-US-08-AC04~AC08·AC15 내부 판정 계약.

평가기가 이미 분류한 인물별 신호만 받아 서버 규칙으로 적격 여부를 정한다. 신호 추출(공급자
호출·프롬프트)과 저장·API 반영은 이 모듈의 범위가 아니다. 신호와 판정에는 원문을 담지 않는다.
잘못되거나 검증을 건너뛴 값은 어떤 경우에도 승인으로 보지 않는다.
"""

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

if TYPE_CHECKING:
    from app.features.creation.save_visibility.rating_review import ReviewSnapshot

AGE_THRESHOLD = 20  # 이 나이 이상만 성인으로 본다(AC04).


class AgeContext(StrEnum):
    ADULT = "adult"  # 맥락상 성인이 분명하다.
    NONE = "none"  # 나이를 알 맥락이 없다.
    CONTRADICTORY = "contradictory"  # 나이 단서가 서로 어긋난다.


class MinorDepiction(StrEnum):
    ABSENT = "absent"
    DEPICTED = "depicted"
    UNCLEAR = "unclear"


class Kinship(StrEnum):
    NONE = "none"
    NON_RESTRICTED = "non_restricted"  # 호칭·인척·무관한 가족(AC08)
    BIOLOGICAL_PARENT_CHILD = "biological_parent_child"
    BIOLOGICAL_SIBLING = "biological_sibling"
    UNCLEAR = "unclear"


RESTRICTED_KINSHIP = frozenset({Kinship.BIOLOGICAL_PARENT_CHILD, Kinship.BIOLOGICAL_SIBLING})


class Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNDETERMINED = "undetermined"
    UNAVAILABLE = "unavailable"


class EligibilityReason(StrEnum):
    UNDERAGE_STATED = "underage_stated"
    MINOR_DEPICTED = "minor_depicted"
    RESTRICTED_KINSHIP = "restricted_kinship"
    AGE_UNDETERMINED = "age_undetermined"
    KINSHIP_UNDETERMINED = "kinship_undetermined"
    INVALID_SIGNALS = "invalid_signals"
    EVALUATOR_FAILURE = "evaluator_failure"


R = EligibilityReason
CONFIRMED = frozenset({R.UNDERAGE_STATED, R.MINOR_DEPICTED, R.RESTRICTED_KINSHIP})
UNDETERMINED = frozenset({R.AGE_UNDETERMINED, R.KINSHIP_UNDETERMINED})
OUTAGE = frozenset({R.INVALID_SIGNALS, R.EVALUATOR_FAILURE})

_STRICT = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")


class CharacterSignals(BaseModel):
    model_config = _STRICT

    character_id: StrictInt
    stated_age: Annotated[StrictInt, Field(ge=0)] | None
    age_context: AgeContext
    minor_depiction: MinorDepiction
    kinship_in_description: Kinship
    kinship_in_relation: Kinship


class EligibilitySignals(BaseModel):
    model_config = _STRICT

    characters: tuple[CharacterSignals, ...]


class EligibilityVerdict(BaseModel):
    model_config = _STRICT

    outcome: Eligibility
    reasons: frozenset[EligibilityReason] = frozenset()

    @model_validator(mode="after")
    def _consistent(self):
        reasons, outcome = self.reasons, self.outcome
        if outcome is Eligibility.ELIGIBLE:
            valid = not reasons
        elif outcome is Eligibility.INELIGIBLE:
            valid = bool(reasons & CONFIRMED) and reasons <= CONFIRMED | UNDETERMINED
        elif outcome is Eligibility.UNDETERMINED:
            valid = bool(reasons) and reasons <= UNDETERMINED
        else:
            valid = bool(reasons) and reasons <= OUTAGE
        if not valid:
            raise ValueError("판정 결과와 사유가 맞지 않는다")
        return self


def unavailable(reason: EligibilityReason = R.EVALUATOR_FAILURE) -> EligibilityVerdict:
    return EligibilityVerdict(outcome=Eligibility.UNAVAILABLE, reasons=frozenset({reason}))


def revalidated[M: BaseModel](model: type[M], value) -> M | None:
    """공개 경계에서 받은 객체를 중첩까지 엄격하게 다시 검증한 사본. 잘못되면 None.

    `model_construct`·`model_copy`는 검증을 건너뛰므로 열거형 대신 문자열·임의 객체나 금지된
    추가 필드가 들어 있을 수 있다. 엄격 모드라 값을 바꿔 맞추지 않고 거부한다. 추가 필드는
    pydantic 재검증 동작에 기대지 않고 직접 한 번 더 확인한다.
    """
    if not isinstance(value, model) or vars(value).keys() - model.model_fields.keys():
        return None
    try:
        return model.model_validate(value, strict=True)
    except (ValidationError, ValueError, TypeError):
        return None


def _character_reasons(c: CharacterSignals) -> set[EligibilityReason]:
    reasons = set()
    if c.stated_age is not None and c.stated_age < AGE_THRESHOLD:
        reasons.add(R.UNDERAGE_STATED)
    if c.minor_depiction is MinorDepiction.DEPICTED:
        reasons.add(R.MINOR_DEPICTED)
    if not reasons and (
        c.minor_depiction is MinorDepiction.UNCLEAR
        or c.age_context is AgeContext.CONTRADICTORY
        or (c.stated_age is None and c.age_context is not AgeContext.ADULT)
    ):
        reasons.add(R.AGE_UNDETERMINED)
    kinship = {c.kinship_in_description, c.kinship_in_relation}
    if kinship & RESTRICTED_KINSHIP:
        reasons.add(R.RESTRICTED_KINSHIP)
    elif Kinship.UNCLEAR in kinship:
        reasons.add(R.KINSHIP_UNDETERMINED)
    return reasons


def decide_eligibility(
    snapshot: "ReviewSnapshot", signals: EligibilitySignals
) -> EligibilityVerdict:
    """모든 인물이 적격이어야 적격이다. 확인된 부적격이 판단 불가보다 앞선다.

    공개 경계라 신호를 다시 검증한다. 검증을 건너뛴 잘못된 신호는 판정하지 않는다.
    """
    checked = revalidated(EligibilitySignals, signals)
    if checked is None:
        return unavailable(R.INVALID_SIGNALS)
    return _decide(snapshot, checked)


def _decide(snapshot: "ReviewSnapshot", signals: EligibilitySignals) -> EligibilityVerdict:
    """검증을 마친 신호만 받는다."""
    expected = sorted(c[0] for c in snapshot.characters)
    # 인물이 없으면 적격 근거가 없다. 빈 신호와 "일치"해도 승인하지 않는다.
    if not expected or sorted(c.character_id for c in signals.characters) != expected:
        return unavailable(R.INVALID_SIGNALS)
    reasons = frozenset().union(*map(_character_reasons, signals.characters))
    if reasons & CONFIRMED:
        outcome = Eligibility.INELIGIBLE
    elif reasons:
        outcome = Eligibility.UNDETERMINED
    else:
        outcome = Eligibility.ELIGIBLE
    return EligibilityVerdict(outcome=outcome, reasons=reasons)


def eligibility_from_output(snapshot: "ReviewSnapshot", output) -> EligibilityVerdict:
    """평가기 출력(JSON 문자열 또는 신호 객체)을 검증해 판정한다. 잘못된 출력은 승인하지 않는다.

    JSON은 전송 형식의 열거형 문자열을 받는다. 파이썬 객체는 `decide_eligibility`와 같은 엄격
    재검증을 거친다(검증을 건너뛴 문자열 열거형·추가 필드를 거부).
    """
    if not isinstance(output, str):
        return decide_eligibility(snapshot, output)
    try:
        signals = EligibilitySignals.model_validate_json(output)
    except (ValidationError, ValueError, TypeError):
        return unavailable(R.INVALID_SIGNALS)
    return _decide(snapshot, signals)  # 방금 검증했으므로 다시 파싱하지 않는다.


def is_eligible(value) -> bool:
    """엄격히 다시 검증한 `EligibilityVerdict`의 적격만 승인이다."""
    checked = revalidated(EligibilityVerdict, value)
    return checked is not None and checked.outcome is Eligibility.ELIGIBLE


class CommonSafety(StrEnum):
    CLEAR = "clear"
    BLOCKED = "blocked"
    UNCHECKED = "unchecked"
    UNAVAILABLE = "unavailable"


class CommonSafetyVerdict(BaseModel):
    model_config = _STRICT

    outcome: CommonSafety


class Offering(StrEnum):
    OFFERABLE = "offerable"
    BLOCKED = "blocked"
    NOT_CLEARED = "not_cleared"


def decide_offering(rating, common, eligibility) -> Offering:
    """AC15 공통 안전 차단이 먼저다. SAFE는 공통 통과만, UNSAFE는 적격까지 필요하다.

    잘못된 공통 판정(추가 필드·검증 건너뜀 포함)은 통과가 아니다.
    """
    checked = revalidated(CommonSafetyVerdict, common)
    outcome = checked.outcome if checked is not None else None
    if outcome is CommonSafety.BLOCKED:
        return Offering.BLOCKED
    if outcome is not CommonSafety.CLEAR:
        return Offering.NOT_CLEARED
    if rating == "SAFE" or (rating == "UNSAFE" and is_eligible(eligibility)):
        return Offering.OFFERABLE
    return Offering.NOT_CLEARED
