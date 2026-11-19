"""CREATE-US-02 표현 설정 입출력. 초과 원문도 draft에 보존한다."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StyleCode = Literal[
    "dynamic", "lyrical", "tense", "humorous", "plain", "delicate", "romantic", "warm"
]


class ExpressionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_of_view: Literal["first_person", "second_person", "third_person"]
    tense: Literal["past", "present"]
    composition: Literal["dialogue", "action", "balanced"]
    response_length: Literal["short", "normal", "long"]
    style_mode: Literal["none", "preset", "custom"]
    preset_styles: list[StyleCode] = Field(max_length=2)
    custom_style: str | None

    @field_validator("custom_style")
    @classmethod
    def check_custom_style(cls, value):
        # 퓨샷 본문과 같은 검사. PostgreSQL text는 NUL을 저장할 수 없어 422로 먼저 거절한다.
        if value is not None and "\x00" in value:
            raise ValueError("저장할 수 없는 문자가 있습니다. 원문을 수정해 주세요.")
        return value

    @model_validator(mode="after")
    def validate_style(self) -> "ExpressionFields":
        if len(set(self.preset_styles)) != len(self.preset_styles):
            raise ValueError("문체 코드는 중복할 수 없습니다.")
        if self.style_mode == "none" and (self.preset_styles or self.custom_style is not None):
            raise ValueError("미선택 문체에 다른 방식의 값을 함께 저장할 수 없습니다.")
        if self.style_mode == "preset" and self.custom_style is not None:
            raise ValueError("기본 문체와 커스텀을 동시에 적용할 수 없습니다.")
        if self.style_mode == "custom" and self.preset_styles:
            raise ValueError("커스텀과 기본 문체를 동시에 적용할 수 없습니다.")
        return self


class ExpressionSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: ExpressionFields


class ExpressionResponse(BaseModel):
    story_id: int
    title: str | None
    save_state: Literal["draft", "private", "public"]
    saved_at: datetime
    expression: ExpressionFields
    request_id: str | None = None


DEFAULT_EXPRESSION = ExpressionFields(
    point_of_view="third_person",
    tense="past",
    composition="balanced",
    response_length="normal",
    style_mode="none",
    preset_styles=[],
    custom_style=None,
)
