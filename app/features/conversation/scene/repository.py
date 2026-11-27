"""CHAT-US-08 세션 현재 장면·캐릭터 상태 갱신과 장면 변경 기록(DB)."""

from psycopg import Connection
from psycopg.types.json import Jsonb


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
               (session_id, generation_id, assistant_message_id, evidence_message_id,
                evidence_from, evidence, before_state, after_state)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
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


def list_changes(conn: Connection, session_id: int):
    return conn.execute(
        """SELECT id, generation_id, assistant_message_id, evidence_message_id, evidence_from,
                  evidence, before_state, after_state, created_at
           FROM conversation_scene_changes WHERE session_id = %s ORDER BY id""",
        (session_id,),
    ).fetchall()
