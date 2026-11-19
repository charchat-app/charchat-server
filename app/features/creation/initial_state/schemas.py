"""CREATE-US-04 초기 상태 계약. 원문 문자열로 보존해 부분·초과 입력도 임시 저장한다."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.features.creation.speech_style.fewshot_schemas import ClientKey, DbId
from app.features.creation.story_basic.schemas import MAX_CHARACTERS_PER_REQUEST

DATE_FIELDS = {
    "calendar": {"year_input", "month_input", "day_input"},
    "world": {"era", "year_input", "month_input", "day_input"},
    "elapsed": {"reference_event", "elapsed_day_input"},
}
ALL_DATE_FIELDS = set().union(*DATE_FIELDS.values())
TIME_FIELDS = ("hour_input", "minute_input")

# 연·월·일·경과일·시·분·호감도 원문 길이 상한. 범위 밖 값(예: 10000)의 임시 저장은 허용하되,
# 아주 긴 숫자 문자열은 형식 오류(422)로 막는다.
NUMBER_INPUT_MAX_LENGTH = 20
NumberInput = Annotated[str, Field(max_length=NUMBER_INPUT_MAX_LENGTH)]

# 기본 정보 요청 상한(캐릭터 50명)에 맞춘 목록 상한. 임시 저장은 등록 조건(1~10명)을 넘는
# 캐릭터 수도 보존하므로 10명 기준으로 막으면 공통 저장 전체가 실패한다.
MAX_PEOPLE = MAX_CHARACTERS_PER_REQUEST + 1  # 캐릭터 + 유저
# 관계: 유저 포함 인물 사이 방향쌍(유저→유저 제외) = 51×50
MAX_RELATIONS = MAX_PEOPLE * (MAX_PEOPLE - 1)
# 호감도: 캐릭터가 출발, 상대는 유저 + 다른 캐릭터 = 50×50
MAX_AFFINITIES = MAX_CHARACTERS_PER_REQUEST * MAX_CHARACTERS_PER_REQUEST


def _clean(value):
    if value is not None and "\x00" in value:
        raise ValueError("저장할 수 없는 문자가 있습니다. 원문을 수정해 주세요.")
    # 빈 입력은 미지정(NULL)으로 저장한다. 공백만 있는 값 등 원문은 바꾸지 않는다.
    return None if value == "" else value


class SceneCharacter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character_id: DbId
    clothing: str | None = None
    inner_thought: str | None = None

    @field_validator("clothing", "inner_thought")
    @classmethod
    def clean_text(cls, value):
        return _clean(value)


class IntroScene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_mode: Literal["calendar", "world", "elapsed"] | None = None
    era: str | None = None
    year_input: NumberInput | None = None
    month_input: NumberInput | None = None
    day_input: NumberInput | None = None
    reference_event: str | None = None
    elapsed_day_input: NumberInput | None = None
    time_format: Literal["24h", "12h"] = "24h"
    hour_input: NumberInput | None = None
    minute_input: NumberInput | None = None
    meridiem: Literal["am", "pm"] | None = None
    place: str | None = None
    characters: list[SceneCharacter] = Field(default_factory=list)

    @field_validator(
        "era",
        "year_input",
        "month_input",
        "day_input",
        "reference_event",
        "elapsed_day_input",
        "hour_input",
        "minute_input",
        "place",
    )
    @classmethod
    def clean_text(cls, value):
        return _clean(value)

    @model_validator(mode="after")
    def check_mode_fields(self):
        # 방식 전환 시 이전 입력을 보관하지 않는다: 선택한 방식의 입력만 허용한다.
        allowed = DATE_FIELDS.get(self.date_mode or "", set())
        if any(getattr(self, name) is not None for name in ALL_DATE_FIELDS - allowed):
            raise ValueError("선택한 날짜 방식의 입력만 저장할 수 있습니다.")
        if self.time_format == "24h" and self.meridiem is not None:
            raise ValueError("24시간제에는 오전·오후를 저장하지 않습니다.")
        ids = [c.character_id for c in self.characters]
        if len(set(ids)) != len(ids):
            raise ValueError("같은 캐릭터 상태를 중복할 수 없습니다.")
        # 착장·속마음이 모두 빈 캐릭터도 요청 배열에 남겨 오류 위치(characters[i])를 요청과
        # 맞춘다. 빈 항목은 저장 직전에 뺀다(filled_characters).
        return self

    def filled_characters(self):
        """착장·속마음 중 하나라도 있는 캐릭터 상태. 모두 빈 캐릭터는 미지정(행 없음)이다."""
        return [c for c in self.characters if c.clothing or c.inner_thought]


class InitialRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_key: ClientKey
    source_kind: Literal["user", "character"]
    source_character_id: DbId | None = None
    target_kind: Literal["user", "character"]
    target_character_id: DbId | None = None
    label: str | None = None

    @field_validator("label")
    @classmethod
    def check_label(cls, value):
        return _clean(value)

    @model_validator(mode="after")
    def check_people(self):
        for kind, character in [
            (self.source_kind, self.source_character_id),
            (self.target_kind, self.target_character_id),
        ]:
            if (kind == "character") != (character is not None):
                raise ValueError("관계 인물 연결을 확인하세요.")
        if (self.source_kind, self.source_character_id) == (
            self.target_kind,
            self.target_character_id,
        ):
            raise ValueError("시작 인물과 대상 인물은 같을 수 없습니다.")
        return self

    @property
    def direction(self):
        return (
            self.source_kind,
            self.source_character_id,
            self.target_kind,
            self.target_character_id,
        )


def unique_relations(relations: list[InitialRelation]) -> list[InitialRelation]:
    keys, directions = set(), set()
    for relation in relations:
        if relation.client_key in keys or relation.direction in directions:
            raise ValueError("같은 방향의 관계는 하나만 저장할 수 있습니다.")
        keys.add(relation.client_key)
        directions.add(relation.direction)
    return relations


class InitialAffinity(BaseModel):
    """출발 캐릭터 → 상대(유저·다른 캐릭터) 방향별 초기 호감도. 관계와 독립이다.

    목록에 없는 조합은 기본값 0으로 시작한다(행을 미리 만들지 않는다).
    원문 보존: 지운 값("")·범위 밖 값도 임시 저장한다.
    유저가 출발점인 호감도는 미정이라 받지 않는다.
    """

    model_config = ConfigDict(extra="forbid")
    source_character_id: DbId
    target_kind: Literal["user", "character"]
    target_character_id: DbId | None = None
    value: NumberInput

    @field_validator("value")
    @classmethod
    def check_value(cls, value):
        if "\x00" in value:
            raise ValueError("저장할 수 없는 문자가 있습니다.")
        return value

    @model_validator(mode="after")
    def check_people(self):
        if (self.target_kind == "character") != (self.target_character_id is not None):
            raise ValueError("호감도 대상 인물 연결을 확인하세요.")
        if self.target_character_id == self.source_character_id:
            raise ValueError("자기 자신에 대한 호감도는 입력할 수 없습니다.")
        return self

    @property
    def direction(self):
        return (self.source_character_id, self.target_kind, self.target_character_id)


def unique_affinities(affinities: list[InitialAffinity]) -> list[InitialAffinity]:
    directions = set()
    for affinity in affinities:
        if affinity.direction in directions:
            raise ValueError("같은 방향의 호감도는 하나만 저장할 수 있습니다.")
        directions.add(affinity.direction)
    return affinities
