"""CHAT-US-02 메시지 전송·응답 엔드포인트."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbConn
from app.features.conversation.messaging import service
from app.features.conversation.messaging.schemas import (
    GenerationResponse,
    GenerationRetryRequest,
    MessageSendRequest,
    TurnsResponse,
)

router = APIRouter(prefix="/conversation", tags=["conversation:messaging"])


@router.get("/sessions/{session_id}/turns")
def get_turns(session_id: int, owner_id: CurrentUserId, conn: DbConn) -> TurnsResponse:
    return service.get_turns(conn, owner_id, session_id)


@router.post("/sessions/{session_id}/messages", status_code=202)
def send_message(
    session_id: int, request: MessageSendRequest, owner_id: CurrentUserId, conn: DbConn
) -> GenerationResponse:
    return service.send_message(conn, owner_id, session_id, request)


@router.get("/generations/{generation_id}")
def get_generation(
    generation_id: int,
    owner_id: CurrentUserId,
    offset: Annotated[int, Query(ge=0)] = 0,
    wait: Annotated[float, Query(ge=0, le=25)] = 0,
) -> GenerationResponse:
    # 롱 폴링은 요청 커넥션(DbConn)을 받지 않는다. 서비스가 확인할 때마다만 커넥션을 빌린다.
    return service.get_generation(owner_id, generation_id, offset, wait)


@router.post("/generations/{generation_id}/retry", status_code=202)
def retry_generation(
    generation_id: int, request: GenerationRetryRequest, owner_id: CurrentUserId, conn: DbConn
) -> GenerationResponse:
    return service.retry_generation(conn, owner_id, generation_id, request)
