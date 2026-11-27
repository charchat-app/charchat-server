"""CHAT-US-02 메시지 전송·응답 API 계약."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

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


class CurrentSceneResponse(BaseModel):
    """CHAT-US-08 누적 현재 장면: 다음 요청의 [현재 장면]이 쓰는 값(상태 카드가 그린다).

    세션 시작 값에 정상 완료된 응답의 info를 오래된 것부터 덮어쓴 결과다. 모양은 info의 읽은 값과
    같고(text 없음), characters는 세션 캐릭터 전원(세션 순서, 모르는 항목은 null)이다.
    date는 날짜 방식이 없거나 날짜 값이 하나도 없으면 null, time은 시가 없으면 null이다.
    """

    date: SceneInfoDateResponse | None
    time: SceneInfoTimeResponse | None
    place: str | None
    characters: list[SceneInfoCharacterResponse]


# ---- CHAT-US-08 info 이용자 수정(KAN-40 결정 C) ----

# 한 요청에 고칠 수 있는 캐릭터 수 상한(요청 크기 제한). 같은 이름은 한 번만 올 수 있다.
INFO_EDIT_CHARACTERS_MAX = 100


def _reject_nul(value: str | None) -> str | None:
    # PostgreSQL text는 NUL을 저장할 수 없어 요청 검증에서 먼저 거절한다.
    if value is not None and "\0" in value:
        raise ValueError("NUL 문자는 사용할 수 없습니다.")
    return value


class SceneInfoDateUpdateRequest(BaseModel):
    """날짜. 세션 날짜 방식(mode)에 맞는 칸으로 그 방식의 날짜 전체를 보낸다(칸별로 잇지 않는다).

    calendar: year·month·day / world: era·year·month·day / elapsed: elapsed_day(+ reference_event).
    elapsed의 reference_event를 빼거나 null로 두면 지금 기준 사건을 그대로 둔다(info 규칙 11).
    mode는 넣지 않아도 되고, 넣으면 세션 날짜 방식과 같아야 한다(날짜 방식은 바꿀 수 없다).
    다른 방식의 칸에 값을 넣으면 거절한다.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["calendar", "world", "elapsed"] | None = None
    era: StrictStr | None = None
    year: StrictInt | None = None
    month: StrictInt | None = None
    day: StrictInt | None = None
    reference_event: StrictStr | None = None
    elapsed_day: StrictInt | None = None

    _nul = field_validator("era", "reference_event")(_reject_nul)


class SceneInfoTimeUpdateRequest(BaseModel):
    """시간. 시·분을 함께 바꾼다. minute을 빼거나 null이면 분을 모르는 시간(`19시`)이다."""

    model_config = ConfigDict(extra="forbid")

    hour: StrictInt
    minute: StrictInt | None = None


class SceneInfoCharacterUpdateRequest(BaseModel):
    """캐릭터 한 명. name은 세션 캐릭터 이름 그대로. 빼거나 null인 항목은 지금 값을 둔다."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    clothing: StrictStr | None = None
    inner_thought: StrictStr | None = None

    _nul = field_validator("name", "clothing", "inner_thought")(_reject_nul)


class SceneInfoUpdateRequest(BaseModel):
    """최신 AI 응답의 info 수정. 모양은 current_scene과 같아 화면 값을 그대로 고쳐 보낼 수 있다.

    빼거나 null로 둔 칸은 그 응답의 지금 값(= 지금 current_scene)을 그대로 둔다.
    값을 지우는 방법은 없다(info 형식은 값을 쓰거나 이어 쓰기만 한다).
    값 규칙·상한은 parse_info와 같고 세션 값(날짜 방식, 캐릭터 이름)이 필요해 서비스에서 검사한다
    (SCENE_INFO_INVALID_VALUE 등).
    """

    model_config = ConfigDict(extra="forbid")

    date: SceneInfoDateUpdateRequest | None = None
    time: SceneInfoTimeUpdateRequest | None = None
    place: StrictStr | None = None
    # 목록에 없는 캐릭터는 그대로 둔다.
    characters: Annotated[
        list[SceneInfoCharacterUpdateRequest], Field(max_length=INFO_EDIT_CHARACTERS_MAX)
    ] = []

    _nul = field_validator("place")(_reject_nul)


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
    # CHAT-US-08: 다음 요청에 쓰일 누적 현재 장면(메시지별 info는 그 응답 하나의 값).
    current_scene: CurrentSceneResponse


class SceneInfoUpdateResponse(BaseModel):
    """info 수정 결과: 고친 응답의 info(턴 API 메시지 info와 같은 모양)와 새 누적 현재 장면."""

    message_id: int
    info: SceneInfoResponse
    current_scene: CurrentSceneResponse
