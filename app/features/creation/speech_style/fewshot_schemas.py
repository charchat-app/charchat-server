"""CREATE-US-02 퓨샷 전체 편집 컬렉션 계약. 배열 순서가 저장 순서다."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def valid_id(value: str) -> str:
    if (
        not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
        or int(value) > 9223372036854775807
    ):
        raise ValueError("양의 bigint 문자열이 필요합니다.")
    return value


DbId = Annotated[str, Field(max_length=19), AfterValidator(valid_id)]
ClientKey = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")]


class FewshotMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: DbId | None = None
    client_key: ClientKey
    speaker_kind: Literal["user", "character", "narrator"]
    character_id: DbId | None = None
    body: str | None = None

    @model_validator(mode="after")
    def check_speaker(self):
        if (self.speaker_kind == "character") != (self.character_id is not None):
            raise ValueError("발화자와 캐릭터 연결을 확인하세요.")
        return self

    @field_validator("body")
    @classmethod
    def check_body(cls, value):
        if value is not None and "\x00" in value:
            raise ValueError("저장할 수 없는 문자가 있습니다. 원문을 수정해 주세요.")
        return value


class FewshotGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: DbId | None = None
    client_key: ClientKey
    messages: list[FewshotMessage]


class FewshotsSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fewshots: list[FewshotGroup]

    @model_validator(mode="after")
    def unique_references(self):
        keys, groups, messages = set(), set(), set()
        for group in self.fewshots:
            for item in [group, *group.messages]:
                if item.client_key in keys:
                    raise ValueError("편집 식별자는 중복할 수 없습니다.")
                keys.add(item.client_key)
            if group.id is not None:
                if group.id in groups:
                    raise ValueError("예시 ID는 중복할 수 없습니다.")
                groups.add(group.id)
            for message in group.messages:
                if message.id is not None:
                    if message.id in messages:
                        raise ValueError("메시지 ID는 중복할 수 없습니다.")
                    messages.add(message.id)
        return self


class FewshotCharacter(BaseModel):
    id: DbId
    name: str | None
    position: int


class FewshotCharactersResponse(BaseModel):
    story_id: DbId
    characters: list[FewshotCharacter]


class FewshotsResponse(BaseModel):
    story_id: DbId
    title: str | None
    save_state: Literal["draft", "private", "public"]
    saved_at: datetime
    characters: list[FewshotCharacter]
    fewshots: list[FewshotGroup]
    request_id: str | None = None
