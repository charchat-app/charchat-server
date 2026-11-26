"""CHAT-US-13 대화 중 모델 변경 엔드포인트."""

from fastapi import APIRouter

from app.api.deps import CurrentUserId, DbConn
from app.features.conversation.model_change import service
from app.features.conversation.model_change.schemas import (
    ModelListResponse,
    SessionModelChangeRequest,
    SessionModelChangeResponse,
)

router = APIRouter(prefix="/conversation", tags=["conversation:model_change"])


@router.get("/models")
def list_models(_owner_id: CurrentUserId, conn: DbConn) -> ModelListResponse:
    # 인증만 확인한다. 모델 목록은 모든 이용자에게 같다.
    return service.list_models(conn)


@router.put("/sessions/{session_id}/model")
def change_session_model(
    session_id: int, request: SessionModelChangeRequest, owner_id: CurrentUserId, conn: DbConn
) -> SessionModelChangeResponse:
    return service.change_session_model(conn, owner_id, session_id, request)
