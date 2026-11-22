"""CHAT-US-02 대화 이력·응답 생성 행과 모델 요청에 쓰는 최신 저장 설정 조회."""

from psycopg import Connection
from psycopg.types.json import Jsonb

ACTIVE_STATUSES = ("pending", "streaming")
FAILED_STATUSES = ("failed", "failed_partial", "save_failed")
ACTIVE_INDEX = "conversation_generations_one_active_idx"

_GENERATION_COLUMNS = """g.id, g.session_id, g.owner_id, g.status, g.user_message_id,
    g.assistant_message_id, g.error_code, g.model, g.billable, g.request_id,
    char_length(g.text) AS text_length"""


def set_consistent_read(conn: Connection) -> None:
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


def get_session(conn: Connection, session_id: int, owner_id: int, *, lock: bool = False):
    # 시작 API(get_session)와 같은 접근 규칙: 삭제 스토리 세션은 읽기 전용으로 남는다.
    query = """
        SELECT cs.id, cs.story_id, cs.model, cs.intro_messages_snapshot,
               s.deleted_at IS NOT NULL AS read_only
        FROM conversation_sessions cs JOIN stories s ON s.id = cs.story_id
        WHERE cs.id = %s AND cs.owner_id = %s
          AND (s.deleted_at IS NOT NULL OR s.save_state = 'public'
               OR (s.owner_id = cs.owner_id AND s.save_state IN ('public', 'private')))
    """
    if lock:
        query += " FOR UPDATE OF cs"
    return conn.execute(query, (session_id, owner_id)).fetchone()


def lock_session_by_id(conn: Connection, session_id: int):
    return conn.execute(
        "SELECT id, model FROM conversation_sessions WHERE id = %s FOR UPDATE", (session_id,)
    ).fetchone()


def set_session_model_if_missing(conn: Connection, session_id: int, model: str) -> str:
    return conn.execute(
        """UPDATE conversation_sessions SET model = COALESCE(model, %s)
           WHERE id = %s RETURNING model""",
        (model, session_id),
    ).fetchone()["model"]


def get_play_name(conn: Connection, session_id: int) -> str:
    row = conn.execute(
        "SELECT name FROM conversation_user_profiles WHERE session_id = %s", (session_id,)
    ).fetchone()
    return row["name"] if row else ""


def list_session_characters(conn: Connection, session_id: int):
    """세션 캐릭터 ID·세션 이름과 원본 캐릭터의 최신 저장 이름."""
    return conn.execute(
        """SELECT cs.id, cs.name_snapshot AS name, c.name AS source_name,
                  cs.clothing, cs.inner_thought
           FROM conversation_character_states cs
           LEFT JOIN characters c ON c.id = cs.source_character_id
           WHERE cs.session_id = %s ORDER BY cs.id""",
        (session_id,),
    ).fetchall()


def get_generation_by_request(conn: Connection, owner_id: int, session_id: int, request_id: str):
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS} FROM conversation_generations g
            WHERE g.owner_id = %s AND g.session_id = %s AND g.request_id = %s""",
        (owner_id, session_id, request_id),
    ).fetchone()


def get_generation(conn: Connection, generation_id: int, owner_id: int | None = None):
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS} FROM conversation_generations g
            WHERE g.id = %s AND (%s::bigint IS NULL OR g.owner_id = %s)""",
        (generation_id, owner_id, owner_id),
    ).fetchone()


def get_generation_with_text(conn: Connection, generation_id: int, offset: int):
    """상태와 offset 이후 원문을 한 번에 읽어 서로 어긋나지 않게 한다.

    substr·char_length는 문자(코드 포인트) 단위이며 substr은 1부터 센다.
    """
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS}, substr(g.text, %s) AS text
            FROM conversation_generations g WHERE g.id = %s""",
        (offset + 1, generation_id),
    ).fetchone()


def get_full_text(conn: Connection, generation_id: int) -> str:
    return conn.execute(
        "SELECT text FROM conversation_generations WHERE id = %s", (generation_id,)
    ).fetchone()["text"]


def get_latest_generation(conn: Connection, session_id: int):
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS} FROM conversation_generations g
            WHERE g.session_id = %s ORDER BY g.id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()


def get_active_generation(conn: Connection, session_id: int):
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS} FROM conversation_generations g
            WHERE g.session_id = %s AND g.status IN ('pending', 'streaming')""",
        (session_id,),
    ).fetchone()


def has_user_message_after(conn: Connection, session_id: int, message_id: int) -> bool:
    return (
        conn.execute(
            """SELECT 1 FROM conversation_messages later
               JOIN conversation_messages m ON m.id = %s
               WHERE later.session_id = %s AND later.role = 'user' AND later.seq > m.seq
               LIMIT 1""",
            (message_id, session_id),
        ).fetchone()
        is not None
    )


def insert_message(conn: Connection, session_id: int, role: str, parts: list[dict]) -> int:
    # 호출자가 세션 행을 잠근 상태에서 다음 순번을 정한다.
    return conn.execute(
        """INSERT INTO conversation_messages (session_id, seq, role, parts)
           SELECT %s, COALESCE(MAX(seq), 0) + 1, %s, %s
           FROM conversation_messages WHERE session_id = %s
           RETURNING id""",
        (session_id, role, Jsonb(parts), session_id),
    ).fetchone()["id"]


def insert_generation(
    conn: Connection,
    session_id: int,
    owner_id: int,
    request_id: str,
    user_message_id: int,
    model: str,
) -> int:
    return conn.execute(
        """INSERT INTO conversation_generations
               (session_id, owner_id, request_id, user_message_id, model)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (session_id, owner_id, request_id, user_message_id, model),
    ).fetchone()["id"]


def get_message(conn: Connection, message_id: int):
    return conn.execute(
        "SELECT id, seq, role, parts, created_at FROM conversation_messages WHERE id = %s",
        (message_id,),
    ).fetchone()


def list_messages(conn: Connection, session_id: int, before_seq: int | None = None):
    return conn.execute(
        """SELECT id, seq, role, parts, created_at FROM conversation_messages
           WHERE session_id = %s AND (%s::int IS NULL OR seq < %s) ORDER BY seq""",
        (session_id, before_seq, before_seq),
    ).fetchall()


def expire_stale_generations(conn: Connection, session_id: int, stale_seconds: float) -> None:
    """서버 중단 등으로 갱신이 멈춘 진행 중 생성을 실패로 정리해 세션이 막히지 않게 한다."""
    conn.execute(
        """UPDATE conversation_generations
           SET status = CASE WHEN text = '' THEN 'failed' ELSE 'failed_partial' END,
               error_code = 'GENERATION_STALE', updated_at = clock_timestamp()
           WHERE session_id = %s AND status IN ('pending', 'streaming')
             AND updated_at < clock_timestamp() - make_interval(secs => %s)""",
        (session_id, stale_seconds),
    )


# ---- 생성 작업(별도 커넥션) ----


def mark_streaming(conn: Connection, generation_id: int):
    """pending인 생성만 streaming으로 바꾼다. 이미 처리 중·종료면 None."""
    return conn.execute(
        """UPDATE conversation_generations SET status = 'streaming', updated_at = clock_timestamp()
           WHERE id = %s AND status = 'pending'
           RETURNING id, session_id, owner_id, user_message_id, model""",
        (generation_id,),
    ).fetchone()


def append_text(conn: Connection, generation_id: int, chunk: str) -> bool:
    """누적 원문 뒤에 붙인다. 진행 중이 아니면(정리됨) False."""
    row = conn.execute(
        """UPDATE conversation_generations
           SET text = text || %s, updated_at = clock_timestamp()
           WHERE id = %s AND status = 'streaming' RETURNING id""",
        (chunk, generation_id),
    ).fetchone()
    return row is not None


def finish_generation(
    conn: Connection,
    generation_id: int,
    status: str,
    *,
    error_code: str | None = None,
    assistant_message_id: int | None = None,
    billable: bool = False,
) -> bool:
    row = conn.execute(
        """UPDATE conversation_generations
           SET status = %s, error_code = %s, assistant_message_id = %s, billable = %s,
               updated_at = clock_timestamp()
           WHERE id = %s AND status = 'streaming' RETURNING id""",
        (status, error_code, assistant_message_id, billable, generation_id),
    ).fetchone()
    return row is not None


def get_story_basics(conn: Connection, story_id: int):
    return conn.execute(
        "SELECT title, subtitle, world_description FROM stories WHERE id = %s", (story_id,)
    ).fetchone()


def list_tags(conn: Connection, story_id: int) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(
            """SELECT t.name FROM story_tags st JOIN tags t ON t.id = st.tag_id
               WHERE st.story_id = %s ORDER BY t.name""",
            (story_id,),
        )
    ]


def list_story_characters(conn: Connection, story_id: int):
    return conn.execute(
        """SELECT name, description FROM characters WHERE story_id = %s ORDER BY position""",
        (story_id,),
    ).fetchall()


def get_expression(conn: Connection, story_id: int):
    return conn.execute(
        """SELECT point_of_view, tense, composition, response_length, style_mode,
                  preset_styles, custom_style
           FROM story_expression_settings WHERE story_id = %s""",
        (story_id,),
    ).fetchone()


def list_fewshot_messages(conn: Connection, story_id: int):
    return conn.execute(
        """SELECT f.id AS fewshot_id, m.speaker_kind, c.name AS speaker_name, m.body
           FROM fewshots f JOIN fewshot_messages m ON m.fewshot_id = f.id
           LEFT JOIN characters c ON c.id = m.character_id
           WHERE f.story_id = %s ORDER BY f.position, f.id, m.position, m.id""",
        (story_id,),
    ).fetchall()


def get_user_profile(conn: Connection, session_id: int):
    return conn.execute(
        "SELECT name, description FROM conversation_user_profiles WHERE session_id = %s",
        (session_id,),
    ).fetchone()


def get_scene(conn: Connection, session_id: int):
    return conn.execute(
        """SELECT date_mode, era, year, month, day, reference_event, elapsed_day, hour, minute,
                  place
           FROM conversation_scene_states WHERE session_id = %s""",
        (session_id,),
    ).fetchone()


def list_affinities(conn: Connection, session_id: int):
    return conn.execute(
        """SELECT src.name_snapshot AS source, a.target_kind, dst.name_snapshot AS target,
                  a.affinity AS value
           FROM conversation_affinities a
           JOIN conversation_character_states src
             ON src.session_id = a.session_id AND src.id = a.source_session_character_id
           LEFT JOIN conversation_character_states dst
             ON dst.session_id = a.session_id AND dst.id = a.target_session_character_id
           WHERE a.session_id = %s ORDER BY a.id""",
        (session_id,),
    ).fetchall()


def list_relationships(conn: Connection, session_id: int):
    return conn.execute(
        """SELECT r.source_kind, src.name_snapshot AS source, r.target_kind,
                  dst.name_snapshot AS target, r.label
           FROM conversation_relationships r
           LEFT JOIN conversation_character_states src
             ON src.session_id = r.session_id AND src.id = r.source_session_character_id
           LEFT JOIN conversation_character_states dst
             ON dst.session_id = r.session_id AND dst.id = r.target_session_character_id
           WHERE r.session_id = %s ORDER BY r.id""",
        (session_id,),
    ).fetchall()
