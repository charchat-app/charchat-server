"""CREATE-US-03 인트로 컬렉션 계약. 배열 순서가 저장 순서이고 첫 번째가 기본 인트로다."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.features.creation.speech_style.fewshot_schemas import ClientKey, DbId, FewshotMessage

MAX_INTROS = 5
INTRO_TITLE_LIMIT = 30
INTRO_BODY_LIMIT = 2000


class Intro(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: DbId | None = None
    client_key: ClientKey
    # 임시 저장에서는 빈 제목·30자 초과도 원문 그대로 보존한다. 완성 저장에서만 검사한다.
    title: str | None = None
    # 말풍선 형식은 2번 퓨샷과 같다.
    messages: list[FewshotMessage]

    @field_validator("title")
    @classmethod
    def check_title(cls, value):
        if value is not None and "\x00" in value:
            raise ValueError("저장할 수 없는 문자가 있습니다. 원문을 수정해 주세요.")
        return value


def unique_references(intros: list[Intro]) -> list[Intro]:
    keys, intro_ids, message_ids = set(), set(), set()
    for intro in intros:
        for item in [intro, *intro.messages]:
            if item.client_key in keys:
                raise ValueError("편집 식별자는 중복할 수 없습니다.")
            keys.add(item.client_key)
        if intro.id is not None:
            if intro.id in intro_ids:
                raise ValueError("인트로 ID는 중복할 수 없습니다.")
            intro_ids.add(intro.id)
        for message in intro.messages:
            if message.id is not None:
                if message.id in message_ids:
                    raise ValueError("메시지 ID는 중복할 수 없습니다.")
                message_ids.add(message.id)
    return intros


IntroList = Annotated[list[Intro], Field(max_length=MAX_INTROS), AfterValidator(unique_references)]
