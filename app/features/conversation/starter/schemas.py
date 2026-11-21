"""CHAT-US-01 대화 시작 API 계약."""

from typing import Literal

from pydantic import BaseModel, field_validator

ProfileKind = Literal["preset", "personal"]


class MessageResponse(BaseModel):
    speaker_kind: Literal["character", "user", "narrator"]
    speaker_name: str | None
    body: str


class CharacterResponse(BaseModel):
    id: int
    name: str


class StoryCharacterResponse(CharacterResponse):
    description: str


class IntroResponse(BaseModel):
    id: int
    title: str
    messages: list[MessageResponse]


class ProfileResponse(BaseModel):
    id: int
    kind: ProfileKind
    name: str
    description: str
    initial_name: str | None


class StoryResponse(BaseModel):
    id: int
    title: str
    subtitle: str
    world_description: str
    effective_rating: str | None
    characters: list[StoryCharacterResponse]
    intros: list[IntroResponse]
    profiles: list[ProfileResponse]
    existing_session_id: int | None
    tags: list[str]


class SessionCreateRequest(BaseModel):
    intro_id: int
    profile_kind: ProfileKind
    profile_id: int
    play_name: str

    @field_validator("play_name")
    @classmethod
    def reject_nul_play_name(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("플레이 이름에는 NUL 문자를 사용할 수 없습니다.")
        return value


class SessionCreatedResponse(BaseModel):
    id: int


class SessionResponse(BaseModel):
    id: int
    story_title: str
    model: str | None
    read_only: bool
    messages: list[MessageResponse]
    characters: list[CharacterResponse]
