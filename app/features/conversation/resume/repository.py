"""CHAT-US-03 소유한 대화 세션 목록 조회."""

from psycopg import Connection

MAX_SESSION_ID = 2**63 - 1


def list_sessions(conn: Connection, owner_id: int, before_id: int | None, limit: int):
    # bigint 범위보다 큰 커서는 모든 세션 ID보다 크므로 상한이 없는 것과 같다.
    if before_id is not None and before_id > MAX_SESSION_ID:
        before_id = None
    # KAN-16 방 상세와 동일한 원본 접근 정책을 조회에 적용한다.
    return conn.execute(
        """SELECT cs.id, cs.story_id, s.title AS story_title,
                  s.deleted_at IS NOT NULL AS read_only
           FROM conversation_sessions cs JOIN stories s ON s.id = cs.story_id
           WHERE cs.owner_id = %s
             AND (%s::bigint IS NULL OR cs.id < %s)
             AND (s.deleted_at IS NOT NULL OR s.save_state = 'public'
                  OR (s.owner_id = cs.owner_id AND s.save_state IN ('public', 'private')))
           ORDER BY cs.id DESC LIMIT %s""",
        (owner_id, before_id, before_id, limit),
    ).fetchall()
