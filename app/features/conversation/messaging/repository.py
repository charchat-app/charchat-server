"""CHAT-US-02 대화 이력·응답 생성 행과 모델 요청에 쓰는 최신 저장 설정 조회."""

from psycopg import Connection

ACTIVE_STATUSES = ("pending", "streaming")
FAILED_STATUSES = ("failed", "failed_partial", "save_failed")
ACTIVE_INDEX = "conversation_generations_one_active_idx"
# 생성 상태 → 팀 표 conversation_messages.status(assistant 메시지).
MESSAGE_STATUS = {
    "pending": "pending",
    "streaming": "streaming",
    "completed": "completed",
    "failed": "failed",
    "save_failed": "failed",
    "failed_partial": "interrupted",
}

# model_name은 생성이 요청한 모델(g.model_id)의 표시 이름이다. 세션의 현재 선택 모델과 다를 수 있다.
_GENERATION_COLUMNS = """g.id, g.session_id, cs.owner_id, g.status, g.user_message_id,
    g.assistant_message_id, g.error_code, g.model_id, gm.name AS model_name, g.reported_model,
    g.billable, g.request_id, char_length(g.text) AS text_length"""
_GENERATION_FROM = """conversation_generations g
    JOIN conversation_sessions cs ON cs.id = g.session_id
    JOIN models gm ON gm.id = g.model_id"""
# 팀 메시지 표에는 시각 컬럼이 없어 생성 작업의 시각을 쓴다. 이용자 메시지는 처음 만든 생성 요청의
# 시각, AI 메시지는 그 생성의 마지막 갱신(완료) 시각이다.
_MESSAGE_CREATED_AT = """COALESCE(
    CASE WHEN m.role = 'user' THEN
        (SELECT min(g.created_at) FROM conversation_generations g
         WHERE g.session_id = m.session_id AND g.user_message_id = m.id)
    ELSE
        (SELECT g.updated_at FROM conversation_generations g
         WHERE g.session_id = m.session_id AND g.assistant_message_id = m.id)
    END, now()) AS created_at"""


def set_consistent_read(conn: Connection) -> None:
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


def get_session(conn: Connection, session_id: int, owner_id: int, *, lock: bool = False):
    # 시작 API(get_session)와 같은 접근 규칙: 삭제 스토리 세션은 읽기 전용으로 남는다.
    if lock:
        # 조인 없이 세션 행만 먼저 잠근다. 조인 쿼리에 FOR UPDATE를 붙이면, 기다리는 동안
        # 모델이 바뀐 경우(CHAT-US-13) 바뀐 행을 옛 models 행과 다시 맞춰 보다가 0행이 되어
        # 세션을 못 찾은 것처럼 404가 난다. 잠금을 얻은 뒤 아래 조회는 커밋된 최신 값을 읽는다.
        locked = conn.execute(
            "SELECT cs.id FROM conversation_sessions cs"
            " WHERE cs.id = %s AND cs.owner_id = %s FOR UPDATE",
            (session_id, owner_id),
        ).fetchone()
        if locked is None:
            return None
    query = """
        SELECT cs.id, cs.story_id, cs.selected_model_id, md.name AS model_name,
               cs.intro_messages_snapshot, s.deleted_at IS NOT NULL AS read_only
        FROM conversation_sessions cs JOIN stories s ON s.id = cs.story_id
        JOIN models md ON md.id = cs.selected_model_id
        WHERE cs.id = %s AND cs.owner_id = %s
          AND (s.deleted_at IS NOT NULL OR s.save_state = 'public'
               OR (s.owner_id = cs.owner_id AND s.save_state IN ('public', 'private')))
    """
    return conn.execute(query, (session_id, owner_id)).fetchone()


def lock_session_by_id(conn: Connection, session_id: int):
    return conn.execute(
        "SELECT id FROM conversation_sessions WHERE id = %s FOR UPDATE", (session_id,)
    ).fetchone()


def get_model(conn: Connection, model_id: int):
    return conn.execute(
        "SELECT id, name, provider, provider_model_id FROM models WHERE id = %s", (model_id,)
    ).fetchone()


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


def get_generation_by_request(conn: Connection, session_id: int, request_id: str):
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS} FROM {_GENERATION_FROM}
            WHERE g.session_id = %s AND g.request_id = %s""",
        (session_id, request_id),
    ).fetchone()


def get_generation(conn: Connection, generation_id: int, owner_id: int | None = None):
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS} FROM {_GENERATION_FROM}
            WHERE g.id = %s AND (%s::bigint IS NULL OR cs.owner_id = %s)""",
        (generation_id, owner_id, owner_id),
    ).fetchone()


def get_generation_with_text(conn: Connection, generation_id: int, offset: int):
    """상태와 offset 이후 원문을 한 번에 읽어 서로 어긋나지 않게 한다.

    substr·char_length는 문자(코드 포인트) 단위이며 substr은 1부터 센다.
    """
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS}, substr(g.text, %s) AS text
            FROM {_GENERATION_FROM} WHERE g.id = %s""",
        (offset + 1, generation_id),
    ).fetchone()


def get_full_text(conn: Connection, generation_id: int) -> str:
    return conn.execute(
        "SELECT text FROM conversation_generations WHERE id = %s", (generation_id,)
    ).fetchone()["text"]


def get_latest_generation(conn: Connection, session_id: int):
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS} FROM {_GENERATION_FROM}
            WHERE g.session_id = %s ORDER BY g.id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()


def get_active_generation(conn: Connection, session_id: int):
    return conn.execute(
        f"""SELECT {_GENERATION_COLUMNS} FROM {_GENERATION_FROM}
            WHERE g.session_id = %s AND g.status IN ('pending', 'streaming')""",
        (session_id,),
    ).fetchone()


def has_user_message_after(conn: Connection, session_id: int, message_id: int) -> bool:
    return (
        conn.execute(
            """SELECT 1 FROM conversation_messages later
               JOIN conversation_messages m ON m.session_id = later.session_id AND m.id = %s
               WHERE later.session_id = %s AND later.role = 'user'
                 AND later.position > m.position
               LIMIT 1""",
            (message_id, session_id),
        ).fetchone()
        is not None
    )


def insert_message(conn: Connection, session_id: int, role: str, status: str) -> int:
    """세션의 다음 순서(0부터)로 메시지 행을 만든다. 호출자가 세션 행을 잠근 상태여야 한다."""
    return conn.execute(
        """INSERT INTO conversation_messages (session_id, position, role, status)
           SELECT %s, COALESCE(MAX(position), -1) + 1, %s, %s
           FROM conversation_messages WHERE session_id = %s
           RETURNING id""",
        (session_id, role, status, session_id),
    ).fetchone()["id"]


def insert_parts(conn: Connection, session_id: int, message_id: int, parts: list[dict]) -> None:
    """부분을 작성 순서(0부터) 그대로 저장한다. character_id는 세션 캐릭터 ID다."""
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO conversation_message_parts
                   (session_id, message_id, position, speaker_kind, session_character_id, body)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            [
                (
                    session_id,
                    message_id,
                    position,
                    part["speaker_kind"],
                    part.get("character_id"),
                    part["body"],
                )
                for position, part in enumerate(parts)
            ],
        )


def insert_generation(
    conn: Connection,
    session_id: int,
    request_id: str,
    user_message_id: int,
    assistant_message_id: int,
    model_id: int,
) -> int:
    return conn.execute(
        """INSERT INTO conversation_generations
               (session_id, request_id, user_message_id, assistant_message_id, model_id)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (session_id, request_id, user_message_id, assistant_message_id, model_id),
    ).fetchone()["id"]


def get_message(conn: Connection, session_id: int, message_id: int):
    return conn.execute(
        f"""SELECT m.id, m.position, m.role, m.status, {_MESSAGE_CREATED_AT}
            FROM conversation_messages m WHERE m.session_id = %s AND m.id = %s""",
        (session_id, message_id),
    ).fetchone()


def list_messages(conn: Connection, session_id: int, before_position: int | None = None):
    """대화 이력: 이용자 메시지와 정상 완료된 AI 메시지(completed)만 순서대로."""
    return conn.execute(
        f"""SELECT m.id, m.position, m.role, m.status, {_MESSAGE_CREATED_AT}
            FROM conversation_messages m
            WHERE m.session_id = %s AND m.status = 'completed'
              AND (%s::bigint IS NULL OR m.position < %s)
            ORDER BY m.position""",
        (session_id, before_position, before_position),
    ).fetchall()


def list_completed_texts(conn: Connection, session_id: int, before_position: int | None = None):
    """CHAT-US-08: 정상 완료된 생성의 assistant 메시지 ID와 생성 원문(맨 끝 info 포함), 순서대로.

    실패·일부 실패·진행 중 생성의 원문은 넣지 않는다. before_position이 있으면 그 앞 메시지만.
    """
    return conn.execute(
        """SELECT m.id AS message_id, m.position, g.text
           FROM conversation_generations g
           JOIN conversation_messages m
             ON m.session_id = g.session_id AND m.id = g.assistant_message_id
           WHERE g.session_id = %s AND g.status = 'completed'
             AND (%s::bigint IS NULL OR m.position < %s)
           ORDER BY m.position""",
        (session_id, before_position, before_position),
    ).fetchall()


def get_reply(conn: Connection, session_id: int, message_id: int):
    """CHAT-US-08 info 수정 대상: 세션의 메시지와 (assistant면) 그 메시지를 만든 생성의 상태·원문.

    생성이 없으면(이용자 메시지) generation_id·generation_status·text는 null이다.
    """
    return conn.execute(
        """SELECT m.id, m.role, m.status, m.position,
                  g.id AS generation_id, g.status AS generation_status, g.text
           FROM conversation_messages m
           LEFT JOIN conversation_generations g
             ON g.session_id = m.session_id AND g.assistant_message_id = m.id
           WHERE m.session_id = %s AND m.id = %s""",
        (session_id, message_id),
    ).fetchone()


def get_latest_completed_reply_id(conn: Connection, session_id: int) -> int | None:
    """CHAT-US-08: 정상 완료된 생성의 assistant 메시지 중 가장 뒤 것의 ID(없으면 None).

    list_completed_texts(현재 장면을 만드는 응답 목록)의 마지막과 같은 조건이다.
    """
    row = conn.execute(
        """SELECT m.id
           FROM conversation_generations g
           JOIN conversation_messages m
             ON m.session_id = g.session_id AND m.id = g.assistant_message_id
           WHERE g.session_id = %s AND g.status = 'completed'
           ORDER BY m.position DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    return row["id"] if row else None


def update_completed_text(conn: Connection, generation_id: int, text: str) -> bool:
    """CHAT-US-08: 완료된 생성의 원문만 바꾼다(info 수정). 상태·청구·시각(updated_at)은 그대로 둔다.

    updated_at은 AI 메시지의 created_at으로 쓰이므로 바꾸지 않는다. 완료가 아니면 False.
    """
    row = conn.execute(
        """UPDATE conversation_generations SET text = %s
           WHERE id = %s AND status = 'completed' RETURNING id""",
        (text, generation_id),
    ).fetchone()
    return row is not None


def list_parts(conn: Connection, session_id: int, message_ids: list[int]) -> dict[int, list]:
    """메시지 ID → 부분 목록(작성 순서). 키 character_id는 세션 캐릭터 ID다."""
    parts: dict[int, list] = {message_id: [] for message_id in message_ids}
    if not message_ids:
        return parts
    for row in conn.execute(
        """SELECT message_id, speaker_kind, session_character_id AS character_id, body
           FROM conversation_message_parts
           WHERE session_id = %s AND message_id = ANY(%s)
           ORDER BY message_id, position""",
        (session_id, message_ids),
    ):
        parts[row["message_id"]].append(row)
    return parts


def lock_stale_generations(conn: Connection, session_id: int, stale_seconds: float):
    """서버 중단 등으로 갱신이 멈춘 진행 중 생성(id, 원문)을 잠가 읽는다. 트랜잭션 안에서 부른다.

    실패 상태(보이는 글이 있는지)는 서비스가 원문으로 정하고 expire_generation으로 바꾼다.
    """
    return conn.execute(
        """SELECT id, text FROM conversation_generations
           WHERE session_id = %s AND status IN ('pending', 'streaming')
             AND updated_at < clock_timestamp() - make_interval(secs => %s)
           ORDER BY id
           FOR UPDATE""",
        (session_id, stale_seconds),
    ).fetchall()


def expire_generation(conn: Connection, generation_id: int, status: str) -> None:
    """lock_stale_generations로 잠근 생성을 GENERATION_STALE 실패로 끝낸다(assistant 메시지도)."""
    conn.execute(
        """WITH expired AS (
               UPDATE conversation_generations
               SET status = %s, error_code = 'GENERATION_STALE', updated_at = clock_timestamp()
               WHERE id = %s AND status IN ('pending', 'streaming')
               RETURNING session_id, assistant_message_id
           )
           UPDATE conversation_messages m
           SET status = %s
           FROM expired e
           WHERE m.session_id = e.session_id AND m.id = e.assistant_message_id""",
        (status, generation_id, MESSAGE_STATUS[status]),
    )


# ---- 생성 작업(별도 커넥션) ----


def mark_streaming(conn: Connection, generation_id: int):
    """pending인 생성만 streaming으로 바꾼다(assistant 메시지도 함께). 이미 처리 중·종료면 None."""
    return conn.execute(
        """WITH g AS (
               UPDATE conversation_generations
               SET status = 'streaming', updated_at = clock_timestamp()
               WHERE id = %s AND status = 'pending'
               RETURNING id, session_id, user_message_id, assistant_message_id, model_id
           ), message AS (
               UPDATE conversation_messages m SET status = 'streaming'
               FROM g WHERE m.session_id = g.session_id AND m.id = g.assistant_message_id
           )
           SELECT g.*, cs.owner_id FROM g JOIN conversation_sessions cs ON cs.id = g.session_id""",
        (generation_id,),
    ).fetchone()


def append_text(
    conn: Connection, generation_id: int, chunk: str, reported_model: str | None = None
) -> bool:
    """누적 원문 뒤에 붙인다. 진행 중이 아니면(정리됨) False.

    공급자가 알려 준 실제 모델 ID가 있으면 처음 값을 함께 남긴다(CHAT-US-13).
    """
    row = conn.execute(
        """UPDATE conversation_generations
           SET text = text || %s, reported_model = COALESCE(reported_model, %s),
               updated_at = clock_timestamp()
           WHERE id = %s AND status = 'streaming' RETURNING id""",
        (chunk, reported_model, generation_id),
    ).fetchone()
    return row is not None


def finish_generation(
    conn: Connection,
    generation_id: int,
    status: str,
    *,
    error_code: str | None = None,
    billable: bool = False,
    reported_model: str | None = None,
) -> bool:
    """streaming인 생성을 끝내고 assistant 메시지 상태를 MESSAGE_STATUS로 맞춘다.

    reported_model은 공급자가 알려 준 실제 모델 ID다. 이미 기록된 값이 있으면 그대로 둔다.
    """
    row = conn.execute(
        """WITH g AS (
               UPDATE conversation_generations
               SET status = %s, error_code = %s, billable = %s,
                   reported_model = COALESCE(reported_model, %s),
                   updated_at = clock_timestamp()
               WHERE id = %s AND status = 'streaming'
               RETURNING id, session_id, assistant_message_id
           ), message AS (
               UPDATE conversation_messages m SET status = %s
               FROM g WHERE m.session_id = g.session_id AND m.id = g.assistant_message_id
           )
           SELECT id FROM g""",
        (status, error_code, billable, reported_model, generation_id, MESSAGE_STATUS[status]),
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
