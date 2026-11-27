"""CHAT-US-08 세션 현재 장면·캐릭터 상태 갱신과 장면 변경 기록(DB)."""

from psycopg import Connection
from psycopg.types.json import Jsonb

MAX_BIGINT = 2**63 - 1


def get_session(conn: Connection, session_id: int, owner_id: int, *, lock: bool = False):
    """대화 시작·전송·모델 변경 API와 같은 접근 규칙으로 세션을 찾는다.

    삭제 스토리의 세션은 읽기 전용(read_only)으로 남는다. lock이면 세션 행을 잠근다
    (전송·재시도·모델 변경과 같은 잠금 순서: 세션 → 장면).
    """
    if session_id > MAX_BIGINT:
        return None
    return conn.execute(
        """SELECT cs.id, s.deleted_at IS NOT NULL AS read_only
           FROM conversation_sessions cs JOIN stories s ON s.id = cs.story_id
           WHERE cs.id = %s AND cs.owner_id = %s
             AND (s.deleted_at IS NOT NULL OR s.save_state = 'public'
                  OR (s.owner_id = cs.owner_id AND s.save_state IN ('public', 'private')))"""
        + (" FOR UPDATE OF cs" if lock else ""),
        (session_id, owner_id),
    ).fetchone()


def has_active_generation(conn: Connection, session_id: int, stale_seconds: float) -> bool:
    """진행 중(pending·streaming) 생성이 있는지. 갱신이 멈춘(stale) 생성은 진행 중이 아니다."""
    return (
        conn.execute(
            """SELECT 1 FROM conversation_generations
               WHERE session_id = %s AND status IN ('pending', 'streaming')
                 AND updated_at >= clock_timestamp() - make_interval(secs => %s)
               LIMIT 1""",
            (session_id, stale_seconds),
        ).fetchone()
        is not None
    )


def get_scene(conn: Connection, session_id: int):
    """잠그지 않고 읽는다(장면 조회 API)."""
    return conn.execute(
        """SELECT date_mode, era, year, month, day, reference_event, elapsed_day, hour, minute,
                  place
           FROM conversation_scene_states WHERE session_id = %s""",
        (session_id,),
    ).fetchone()


def list_character_states(conn: Connection, session_id: int):
    """잠그지 않고 읽는다(장면 조회 API). 이름은 세션 이름(name_snapshot)이다."""
    return conn.execute(
        """SELECT id, name_snapshot AS name, clothing, inner_thought
           FROM conversation_character_states WHERE session_id = %s ORDER BY id""",
        (session_id,),
    ).fetchall()


def get_scene_for_update(conn: Connection, session_id: int):
    return conn.execute(
        """SELECT date_mode, era, year, month, day, reference_event, elapsed_day, hour, minute,
                  place
           FROM conversation_scene_states WHERE session_id = %s FOR UPDATE""",
        (session_id,),
    ).fetchone()


def list_character_states_for_update(conn: Connection, session_id: int):
    """세션 캐릭터 상태. 이름은 판단에 보여 준 세션 이름(name_snapshot)이다."""
    return conn.execute(
        """SELECT id, name_snapshot AS name, clothing, inner_thought
           FROM conversation_character_states
           WHERE session_id = %s ORDER BY id FOR UPDATE""",
        (session_id,),
    ).fetchall()


def update_scene(conn: Connection, session_id: int, scene: dict) -> None:
    conn.execute(
        """UPDATE conversation_scene_states
           SET era = %(era)s, year = %(year)s, month = %(month)s, day = %(day)s,
               reference_event = %(reference_event)s, elapsed_day = %(elapsed_day)s,
               hour = %(hour)s, minute = %(minute)s, place = %(place)s
           WHERE session_id = %(session_id)s""",
        {**scene, "session_id": session_id},
    )


def update_character_state(conn: Connection, session_id: int, state: dict) -> None:
    conn.execute(
        """UPDATE conversation_character_states SET clothing = %s, inner_thought = %s
           WHERE session_id = %s AND id = %s""",
        (state["clothing"], state["inner_thought"], session_id, state["id"]),
    )


def insert_change(
    conn: Connection,
    *,
    session_id: int,
    generation_id: int,
    assistant_message_id: int,
    evidence_message_id: int,
    evidence_from: str,
    evidence: str,
    before: dict,
    after: dict,
) -> int:
    return conn.execute(
        """INSERT INTO conversation_scene_changes
               (session_id, source, generation_id, assistant_message_id, evidence_message_id,
                evidence_from, evidence, before_state, after_state)
           VALUES (%s, 'judge', %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            session_id,
            generation_id,
            assistant_message_id,
            evidence_message_id,
            evidence_from,
            evidence,
            Jsonb(before),
            Jsonb(after),
        ),
    ).fetchone()["id"]


def insert_user_edit_change(conn: Connection, *, session_id: int, before: dict, after: dict) -> int:
    """이용자가 장면을 직접 고친 기록. 생성·메시지·근거는 없다."""
    return conn.execute(
        """INSERT INTO conversation_scene_changes (session_id, source, before_state, after_state)
           VALUES (%s, 'user_edit', %s, %s) RETURNING id""",
        (session_id, Jsonb(before), Jsonb(after)),
    ).fetchone()["id"]


def list_changes(conn: Connection, session_id: int):
    return conn.execute(
        """SELECT id, source, generation_id, assistant_message_id, evidence_message_id,
                  evidence_from, evidence, before_state, after_state, created_at
           FROM conversation_scene_changes WHERE session_id = %s ORDER BY id""",
        (session_id,),
    ).fetchall()
