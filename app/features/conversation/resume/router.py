"""CHAT-US-03 내 대화 목록·요청 ID 조회 엔드포인트."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbConn
from app.features.conversation.messaging.schemas import REQUEST_ID_PATTERN
from app.features.conversation.resume import service
from app.features.conversation.resume.schemas import (
    GenerationLookupResponse,
    SessionListResponse,
)

router = APIRouter(prefix="/conversation", tags=["conversation:resume"])


@router.get("/sessions")
def list_sessions(
    owner_id: CurrentUserId,
    conn: DbConn,
    before_id: Annotated[int | None, Query(gt=0)] = None,
) -> SessionListResponse:
    return service.list_sessions(conn, owner_id, before_id)


@router.get("/sessions/{session_id}/generations")
def find_generations(
    session_id: int,
    owner_id: CurrentUserId,
    conn: DbConn,
    request_id: Annotated[str, Query(pattern=REQUEST_ID_PATTERN)],
) -> GenerationLookupResponse:
    return service.find_generations(conn, owner_id, session_id, request_id)
