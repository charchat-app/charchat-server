"""CHAT-US-03 대화 세션 목록 엔드포인트."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbConn
from app.features.conversation.resume import service
from app.features.conversation.resume.schemas import SessionListResponse

router = APIRouter(prefix="/conversation", tags=["conversation:resume"])


@router.get("/sessions")
def list_sessions(
    owner_id: CurrentUserId,
    conn: DbConn,
    before_id: Annotated[int | None, Query(gt=0)] = None,
) -> SessionListResponse:
    return service.list_sessions(conn, owner_id, before_id)
