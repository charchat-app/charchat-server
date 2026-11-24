"""CHAT-US-03 소유한 대화 세션 목록·요청 ID 조회."""

from psycopg import Connection

MAX_SESSION_ID = 2**63 - 1

# KAN-16 방 상세와 같은 원본 접근 정책. 삭제 스토리의 세션은 읽기 전용으로 남는다.
_ACCESSIBLE = """(s.deleted_at IS NOT NULL OR s.save_state = 'public'
                  OR (s.owner_id = cs.owner_id AND s.save_state IN ('public', 'private')))"""


def list_sessions(
    conn: Connection, owner_id: int, before_id: int | None, limit: int, stale_seconds: float
):
    """세션마다 표시 이름·모델·진행 중 생성 여부·마지막 저장 메시지 시각을 함께 읽는다.

    진행 중 여부는 읽기만 한다. 멈춘 생성의 실패 정리는 대화방 조회(CHAT-US-02)가 맡는다.
    """
    # bigint 범위보다 큰 커서는 모든 세션 ID보다 크므로 상한이 없는 것과 같다.
    if before_id is not None and before_id > MAX_SESSION_ID:
        before_id = None
    return conn.execute(
        f"""SELECT cs.id, cs.story_id, s.title AS story_title,
                   s.deleted_at IS NOT NULL AS read_only,
                   md.name AS model, up.name AS play_name,
                   EXISTS (
                       SELECT 1 FROM conversation_generations g
                       WHERE g.session_id = cs.id AND g.status IN ('pending', 'streaming')
                         AND g.updated_at >= clock_timestamp() - make_interval(secs => %s)
                   ) AS generating,
                   (SELECT max(g.updated_at) FROM conversation_generations g
                    WHERE g.session_id = cs.id) AS last_activity_at
            FROM conversation_sessions cs JOIN stories s ON s.id = cs.story_id
            JOIN models md ON md.id = cs.selected_model_id
            JOIN conversation_user_profiles up ON up.session_id = cs.id
            WHERE cs.owner_id = %s
              AND (%s::bigint IS NULL OR cs.id < %s)
              AND {_ACCESSIBLE}
            ORDER BY cs.id DESC LIMIT %s""",
        (stale_seconds, owner_id, before_id, before_id, limit),
    ).fetchall()


def list_last_completed_parts(conn: Connection, session_ids: list[int]):
    """세션마다 마지막 완료(completed) 메시지의 부분들. 진행·실패 중 원문은 읽지 않는다."""
    if not session_ids:
        return []
    return conn.execute(
        """SELECT p.session_id, p.speaker_kind, p.body
           FROM (
               SELECT DISTINCT ON (m.session_id) m.session_id, m.id
               FROM conversation_messages m
               WHERE m.session_id = ANY(%s) AND m.status = 'completed'
               ORDER BY m.session_id, m.position DESC
           ) last
           JOIN conversation_message_parts p
             ON p.session_id = last.session_id AND p.message_id = last.id
           ORDER BY p.session_id, p.position""",
        (session_ids,),
    ).fetchall()


def is_accessible_session(conn: Connection, session_id: int, owner_id: int) -> bool:
    row = conn.execute(
        f"""SELECT 1 FROM conversation_sessions cs JOIN stories s ON s.id = cs.story_id
            WHERE cs.id = %s AND cs.owner_id = %s AND {_ACCESSIBLE}""",
        (session_id, owner_id),
    ).fetchone()
    return row is not None


def find_generation_id(conn: Connection, session_id: int, request_id: str) -> int | None:
    row = conn.execute(
        """SELECT id FROM conversation_generations
           WHERE session_id = %s AND request_id = %s""",
        (session_id, request_id),
    ).fetchone()
    return row["id"] if row else None
