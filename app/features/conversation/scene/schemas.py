"""CHAT-US-08 현재 장면 조회·이용자 직접 수정 API 계약(KAN-22).

요청은 화면 폼이 현재 값을 채워 보내는 전체 교체다. 모든 키가 필수이고(빠지면 요청 검증 오류),
값은 null(미지정)을 허용한다. 숫자는 정수만(문자열·불리언·실수 거절), 글자는 문자열만 받는다.
날짜 방식(date_mode)은 바꿀 수 없어 요청에 두지 않는다. 정의되지 않은 키는 거절한다.
범위·글자 수·날짜 방식별 규칙은 세션 값이 필요해 서비스에서 검사한다(CONVERSATION_SCENE_INVALID).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

# 한 요청에 고칠 수 있는 캐릭터 수 상한(요청 크기 제한).
CHARACTERS_MAX = 100


class SceneCharacterResponse(BaseModel):
    # 세션 캐릭터 상태 ID(conversation_character_states.id). 수정 요청에 그대로 쓴다.
    id: int
    # 세션 시작 때의 이름(name_snapshot).
    name: str | None
    clothing: str | None
    inner_thought: str | None


class SceneResponse(BaseModel):
    date_mode: Literal["calendar", "world", "elapsed"] | None
    era: str | None
    year: int | None
    month: int | None
    day: int | None
    reference_event: str | None
    elapsed_day: int | None
    # 24시간제(0~23).
    hour: int | None
    minute: int | None
    place: str | None
    characters: list[SceneCharacterResponse]
    # 삭제 스토리의 대화: 볼 수만 있고 고칠 수 없다.
    read_only: bool


class SceneCharacterUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt
    clothing: StrictStr | None
    inner_thought: StrictStr | None


class SceneUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    era: StrictStr | None
    year: StrictInt | None
    month: StrictInt | None
    day: StrictInt | None
    reference_event: StrictStr | None
    elapsed_day: StrictInt | None
    hour: StrictInt | None
    minute: StrictInt | None
    place: StrictStr | None
    # 목록에 없는 캐릭터는 그대로 둔다. 같은 ID는 한 번만 올 수 있어 세션 캐릭터 수를 넘지 않는다.
    characters: Annotated[list[SceneCharacterUpdateRequest], Field(max_length=CHARACTERS_MAX)]
