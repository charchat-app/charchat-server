"""CHAT-US-08 현재 장면 조회·이용자 직접 수정 엔드포인트(KAN-22)."""

from fastapi import APIRouter

from app.api.deps import CurrentUserId, DbConn
from app.features.conversation.scene import service
from app.features.conversation.scene.schemas import SceneResponse, SceneUpdateRequest

router = APIRouter(prefix="/conversation", tags=["conversation:scene"])


@router.get("/sessions/{session_id}/scene")
def get_scene(session_id: int, owner_id: CurrentUserId, conn: DbConn) -> SceneResponse:
    return service.get_scene(conn, owner_id, session_id)


@router.put("/sessions/{session_id}/scene")
def update_scene(
    session_id: int, request: SceneUpdateRequest, owner_id: CurrentUserId, conn: DbConn
) -> SceneResponse:
    return service.update_scene(conn, owner_id, session_id, request)
