"""CHAT-US-13 모델 목록 조회와 세션 선택 모델 변경."""

from psycopg import Connection

MAX_BIGINT = 2**63 - 1


def list_models(conn: Connection):
    return conn.execute("SELECT id, name FROM models ORDER BY id").fetchall()


def list_model_ids_by_name(conn: Connection, name: str) -> list[int]:
    return [
        row["id"]
        for row in conn.execute("SELECT id FROM models WHERE name = %s ORDER BY id", (name,))
    ]


def get_model(conn: Connection, model_id: int):
    if model_id > MAX_BIGINT:
        return None
    return conn.execute("SELECT id, name FROM models WHERE id = %s", (model_id,)).fetchone()


def lock_session(conn: Connection, session_id: int, owner_id: int):
    """대화 시작·전송 API와 같은 접근 규칙으로 세션 행을 잠근다(전송·재시도와 같은 잠금 순서).

    삭제 스토리의 세션은 읽기 전용(read_only)으로 남는다.
    """
    if session_id > MAX_BIGINT:
        return None
    return conn.execute(
        """SELECT cs.id, cs.selected_model_id, s.deleted_at IS NOT NULL AS read_only
           FROM conversation_sessions cs JOIN stories s ON s.id = cs.story_id
           WHERE cs.id = %s AND cs.owner_id = %s
             AND (s.deleted_at IS NOT NULL OR s.save_state = 'public'
                  OR (s.owner_id = cs.owner_id AND s.save_state IN ('public', 'private')))
           FOR UPDATE OF cs""",
        (session_id, owner_id),
    ).fetchone()


def set_selected_model(conn: Connection, session_id: int, model_id: int) -> None:
    conn.execute(
        "UPDATE conversation_sessions SET selected_model_id = %s WHERE id = %s",
        (model_id, session_id),
    )


def get_active_generation(conn: Connection, session_id: int, stale_seconds: float):
    """진행 중(pending·streaming) 생성과 그 생성이 요청한 모델 이름. 읽기만 한다.

    갱신이 멈춘 생성은 진행 중으로 보지 않는다(정리는 대화방 조회·전송이 맡는다).
    """
    return conn.execute(
        """SELECT g.id, m.name AS model
           FROM conversation_generations g JOIN models m ON m.id = g.model_id
           WHERE g.session_id = %s AND g.status IN ('pending', 'streaming')
             AND g.updated_at >= clock_timestamp() - make_interval(secs => %s)""",
        (session_id, stale_seconds),
    ).fetchone()
