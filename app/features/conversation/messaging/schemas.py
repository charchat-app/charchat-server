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


class SceneInfoDateResponse(BaseModel):
    """CHAT-US-08 info의 날짜. mode(세션 날짜 방식)에 해당하는 칸만 채운다.

    calendar: year·month·day / world: era·year·month·day /
    elapsed: elapsed_day와 reference_event(info에 기준 사건을 쓰지 않았으면 null).
    """

    mode: Literal["calendar", "world", "elapsed"]
    era: str | None
    year: int | None
    month: int | None
    day: int | None
    reference_event: str | None
    elapsed_day: int | None


class SceneInfoTimeResponse(BaseModel):
    hour: int  # 0~23
    minute: int | None  # 분을 모르면(`19시`) null


class SceneInfoCharacterResponse(BaseModel):
    name: str  # 세션 캐릭터 이름
    clothing: str | None
    inner_thought: str | None


class SceneInfoResponse(BaseModel):
    """CHAT-US-08 AI 응답 맨 끝 ```info 블록. text는 원문(줄바꿈은 \n), 나머지는 읽은 값.

    읽지 못했거나 적지 않은 항목은 null(characters는 읽은 캐릭터만). 규칙은 messaging/info.py.
    """

    text: str
    date: SceneInfoDateResponse | None
    time: SceneInfoTimeResponse | None
    place: str | None
    characters: list[SceneInfoCharacterResponse]


class ChatMessageResponse(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    parts: list[MessagePartResponse]
    created_at: datetime
    # CHAT-US-08: assistant 응답 맨 끝 info 블록(현재 장면). 이용자 메시지·info 없는 응답은 null.
    # parts에는 info가 없다. 표시 ON/OFF와 관계없이 다음 요청의 현재 장면에 쓰인다.
    info: SceneInfoResponse | None = None


class GenerationResponse(BaseModel):
    id: int
    session_id: int
    status: GenerationStatus
    user_message_id: int
    assistant_message: ChatMessageResponse | None
    # offset 이후 누적 원문과 전체 길이(유니코드 코드 포인트).
    # CHAT-US-08: 원문 그대로라 맨 끝 ```info 블록도 들어 있다. 앱은 info.py 규칙으로 숨긴다.
    text: str
    next_offset: int
    error_code: str | None
    retryable: bool
    # 이 생성이 요청한 모델의 표시 이름(models.name of model_id, CHAT-US-13). 진행 중 생성은
    # 세션 모델을 바꿔도 이 모델로 끝까지 진행한다.
    model: str


class TurnsResponse(BaseModel):
    messages: list[ChatMessageResponse]
    active_generation: GenerationResponse | None
    last_failed_generation: GenerationResponse | None
    model: str
    # 세션의 현재 선택 모델 id(CHAT-US-13). 다음 전송·재시도가 이 모델로 요청한다.
    model_id: int
