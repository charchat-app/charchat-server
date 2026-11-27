"""CHAT-US-01 대화 시작 엔드포인트."""

from fastapi import APIRouter

from app.api.deps import CommonSafetyDep, CurrentUserId, DbConn
from app.features.conversation.starter import service
from app.features.conversation.starter.schemas import (
    SessionCreatedResponse,
    SessionCreateRequest,
    SessionResponse,
    StoryResponse,
)

router = APIRouter(prefix="/conversation", tags=["conversation:starter"])


@router.get("/stories/{story_id}")
def get_story(
    story_id: int, owner_id: CurrentUserId, conn: DbConn, guard: CommonSafetyDep
) -> StoryResponse:
    return service.get_story(conn, owner_id, story_id, guard)


@router.post("/stories/{story_id}/sessions", status_code=201)
def create_session(
    story_id: int,
    request: SessionCreateRequest,
    owner_id: CurrentUserId,
    conn: DbConn,
    guard: CommonSafetyDep,
) -> SessionCreatedResponse:
    return service.create_session(conn, owner_id, story_id, request, guard)


@router.get("/sessions/{session_id}")
def get_session(
    session_id: int, owner_id: CurrentUserId, conn: DbConn, guard: CommonSafetyDep
) -> SessionResponse:
    return service.get_session(conn, owner_id, session_id, guard)
