"""CHAT-US-03 대화 세션 목록 응답 구성."""

from psycopg import Connection

from app.features.conversation.resume import repository
from app.features.conversation.resume.schemas import SessionListResponse, SessionSummaryResponse

SESSION_PAGE_SIZE = 30


def list_sessions(conn: Connection, owner_id: int, before_id: int | None) -> SessionListResponse:
    rows = repository.list_sessions(conn, owner_id, before_id, SESSION_PAGE_SIZE + 1)
    sessions = [
        SessionSummaryResponse(
            id=row["id"],
            story_id=row["story_id"],
            story_title=row["story_title"] or "",
            read_only=row["read_only"],
        )
        for row in rows[:SESSION_PAGE_SIZE]
    ]
    return SessionListResponse(
        sessions=sessions,
        next_before_id=sessions[-1].id if len(rows) > SESSION_PAGE_SIZE else None,
    )
