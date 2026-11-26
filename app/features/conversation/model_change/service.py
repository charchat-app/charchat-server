"""CHAT-US-13 대화 중 모델 변경 규칙.

세션의 선택 모델(selected_model_id)만 바꾼다. 생성 행·메시지는 건드리지 않는다. 진행 중 생성은
생성 행에 고정된 model_id로 끝까지 진행하고(AC02), 다음 전송·재시도부터 새 모델로 요청한다(AC01).
모델 변경 이력 표는 두지 않는다(생성 행의 model_id가 요청 모델 기록이다).
"""

from psycopg import Connection

from app.core.config import settings
from app.core.exceptions import AppError
from app.features.conversation.model_change import repository
from app.features.conversation.model_change.schemas import (
    ActiveGenerationModelResponse,
    ModelListResponse,
    ModelResponse,
    SessionModelChangeRequest,
    SessionModelChangeResponse,
)


class SessionNotFoundError(AppError):
    status = 404
    code = "CONVERSATION_SESSION_NOT_FOUND"
    title = "접근할 수 있는 대화 세션을 찾을 수 없습니다"


class ConversationReadOnlyError(AppError):
    status = 409
    code = "CONVERSATION_READ_ONLY"
    title = "삭제된 스토리의 대화는 이어서 진행할 수 없습니다"


class ModelNotFoundError(AppError):
    status = 422
    code = "CONVERSATION_MODEL_NOT_FOUND"
    title = "선택할 수 없는 모델입니다"


def _default_model_id(conn: Connection) -> int | None:
    """CHAT_DEFAULT_MODEL_NAME과 이름이 같은 models 행 하나. 없거나 여러 개면 None."""
    ids = repository.list_model_ids_by_name(conn, settings.chat_default_model_name)
    return ids[0] if len(ids) == 1 else None


def list_models(conn: Connection) -> ModelListResponse:
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = repository.list_models(conn)
        default_id = _default_model_id(conn)
    return ModelListResponse(
        models=[ModelResponse(id=row["id"], name=row["name"]) for row in rows],
        default_model_id=default_id,
    )


def change_session_model(
    conn: Connection, owner_id: int, session_id: int, request: SessionModelChangeRequest
) -> SessionModelChangeResponse:
    with conn.transaction():
        # 전송·재시도와 같이 세션 행을 먼저 잠가, 새 생성 행이 바꾼 뒤의 모델을 확실히 쓴다.
        session = repository.lock_session(conn, session_id, owner_id)
        if session is None:
            raise SessionNotFoundError()
        if session["read_only"]:
            raise ConversationReadOnlyError()
        model = repository.get_model(conn, request.model_id)
        if model is None:
            raise ModelNotFoundError()
        # 같은 모델이면 그대로 둔다(멱등).
        if session["selected_model_id"] != model["id"]:
            repository.set_selected_model(conn, session_id, model["id"])
        active = repository.get_active_generation(
            conn, session_id, settings.chat_generation_stale_seconds
        )
    return SessionModelChangeResponse(
        model_id=model["id"],
        model=model["name"],
        active_generation=(
            ActiveGenerationModelResponse(id=active["id"], model=active["model"])
            if active
            else None
        ),
    )
