"""CHAT-US-03 기존 대화 세션 목록 API 계약."""

from pydantic import BaseModel


class SessionSummaryResponse(BaseModel):
    id: int
    story_id: int
    story_title: str
    read_only: bool


class SessionListResponse(BaseModel):
    sessions: list[SessionSummaryResponse]
    next_before_id: int | None
