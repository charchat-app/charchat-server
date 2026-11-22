"""CHAT-US-02 메시지 전송·응답 API 계약."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SpeakerKind = Literal["user", "character", "narrator"]
GenerationStatus = Literal[
    "pending", "streaming", "completed", "failed", "failed_partial", "save_failed"
]
REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


class MessagePartRequest(BaseModel):
    speaker_kind: SpeakerKind
    # speaker_kind가 character일 때만 넣는 세션 캐릭터 ID(SessionResponse.characters의 id).
    character_id: int | None = None
    body: str

    @field_validator("body")
    @classmethod
    def reject_nul_body(cls, value: str) -> str:
        # PostgreSQL text는 NUL을 저장할 수 없어 요청 검증에서 먼저 거절한다.
        if "\0" in value:
            raise ValueError("메시지에는 NUL 문자를 사용할 수 없습니다.")
        return value


class MessageSendRequest(BaseModel):
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    parts: list[MessagePartRequest]


class GenerationRetryRequest(BaseModel):
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)


class MessagePartResponse(BaseModel):
    speaker_kind: SpeakerKind
    character_id: int | None
    speaker_name: str | None
    body: str


class ChatMessageResponse(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    parts: list[MessagePartResponse]
    created_at: datetime


class GenerationResponse(BaseModel):
    id: int
    session_id: int
    status: GenerationStatus
    user_message_id: int
    assistant_message: ChatMessageResponse | None
    # offset 이후 누적 원문과 전체 길이(유니코드 코드 포인트).
    text: str
    next_offset: int
    error_code: str | None
    retryable: bool


class TurnsResponse(BaseModel):
    messages: list[ChatMessageResponse]
    active_generation: GenerationResponse | None
    last_failed_generation: GenerationResponse | None
    model: str
