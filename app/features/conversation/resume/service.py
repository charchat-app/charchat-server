"""CHAT-US-03 내 대화 목록과 기존 요청 ID로 생성 상태 확인."""

from collections import defaultdict

from psycopg import Connection

from app.core.config import settings
from app.core.exceptions import AppError
from app.features.conversation.messaging import service as messaging_service
from app.features.conversation.resume import repository
from app.features.conversation.resume.schemas import (
    GenerationLookupResponse,
    SessionListResponse,
    SessionSummaryResponse,
)

SESSION_PAGE_SIZE = 30
PREVIEW_LENGTH = 80


class SessionNotFoundError(AppError):
    status = 404
    code = "CONVERSATION_SESSION_NOT_FOUND"
    title = "접근할 수 있는 대화 세션을 찾을 수 없습니다"


def _preview(parts) -> str | None:
    text = " ".join(" ".join(part["body"].split()) for part in parts).strip()
    if not text:
        return None
    chars = list(text)
    return text if len(chars) <= PREVIEW_LENGTH else "".join(chars[:PREVIEW_LENGTH]) + "…"


def list_sessions(conn: Connection, owner_id: int, before_id: int | None) -> SessionListResponse:
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = repository.list_sessions(
            conn,
            owner_id,
            before_id,
            SESSION_PAGE_SIZE + 1,
            settings.chat_generation_stale_seconds,
        )
        has_more = len(rows) > SESSION_PAGE_SIZE
        rows = rows[:SESSION_PAGE_SIZE]
        parts = defaultdict(list)
        for part in repository.list_last_completed_parts(conn, [row["id"] for row in rows]):
            parts[part["session_id"]].append(part)
    sessions = [
        SessionSummaryResponse(
            id=row["id"],
            story_id=row["story_id"],
            story_title=row["story_title"] or "",
            read_only=row["read_only"],
            model=row["model"],
            play_name=row["play_name"],
            generating=row["generating"],
            last_message_preview=_preview(parts[row["id"]]),
            last_activity_at=row["last_activity_at"],
        )
        for row in rows
    ]
    return SessionListResponse(
        sessions=sessions, next_before_id=sessions[-1].id if has_more else None
    )


def find_generations(
    conn: Connection, owner_id: int, session_id: int, request_id: str
) -> GenerationLookupResponse:
    """재접속 때 결과를 모르는 전송을 새로 만들지 않고 기존 request_id로 상태만 확인한다(AC09).

    찾으면 CHAT-US-02 생성 조회와 같은 응답(처음부터의 원문·상태·완료 메시지)을 돌려준다.
    """
    if not repository.is_accessible_session(conn, session_id, owner_id):
        raise SessionNotFoundError()
    generation_id = repository.find_generation_id(conn, session_id, request_id)
    if generation_id is None:
        return GenerationLookupResponse(generations=[])
    return GenerationLookupResponse(
        generations=[messaging_service.get_generation(owner_id, generation_id, 0, 0)]
    )
