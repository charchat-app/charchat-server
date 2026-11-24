"""CHAT-US-03 내 대화 목록·요청 ID 조회 API 계약."""

from datetime import datetime

from pydantic import BaseModel

from app.features.conversation.messaging.schemas import GenerationResponse


class SessionSummaryResponse(BaseModel):
    id: int
    story_id: int
    story_title: str
    read_only: bool
    # 세션에 고정된 모델의 표시 이름(models.name)과 적용된 플레이 이름.
    model: str
    play_name: str
    # 응답을 만드는 중인 생성이 있으면 true(AC11).
    generating: bool
    # 마지막 완료 메시지의 앞부분. 진행·실패 중 원문은 넣지 않는다.
    last_message_preview: str | None
    last_activity_at: datetime | None


class SessionListResponse(BaseModel):
    sessions: list[SessionSummaryResponse]
    next_before_id: int | None


class GenerationLookupResponse(BaseModel):
    # 같은 세션의 request_id는 하나뿐이라 0개 또는 1개다.
    generations: list[GenerationResponse]
