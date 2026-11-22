"""CHAT-US-01 저장된 스토리·인트로·프로필 조회."""

from psycopg import Connection
from psycopg.types.json import Jsonb


def set_consistent_read(conn: Connection) -> None:
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


def get_story(conn: Connection, story_id: int, owner_id: int, *, lock: bool = False):
    query = """
        SELECT id, title, subtitle, world_description
        FROM stories
        WHERE id = %s AND deleted_at IS NULL
          AND (save_state = 'public' OR (save_state = 'private' AND owner_id = %s))
    """
    if lock:
        query += " FOR UPDATE"
    return conn.execute(query, (story_id, owner_id)).fetchone()


def list_characters(conn: Connection, story_id: int):
    return conn.execute(
        """SELECT id, name, description FROM characters
           WHERE story_id = %s ORDER BY position""",
        (story_id,),
    ).fetchall()


def list_intros(conn: Connection, story_id: int):
    return conn.execute(
        "SELECT id, title FROM intros WHERE story_id = %s ORDER BY position", (story_id,)
    ).fetchall()


def list_intro_messages(conn: Connection, story_id: int, intro_id: int | None = None):
    return conn.execute(
        """SELECT i.id AS intro_id, m.speaker_kind, c.name AS speaker_name, m.body
           FROM intros i JOIN intro_messages m ON m.intro_id = i.id
           LEFT JOIN characters c ON c.id = m.character_id AND c.story_id = i.story_id
           WHERE i.story_id = %s AND (%s::bigint IS NULL OR i.id = %s)
           ORDER BY i.position, m.position""",
        (story_id, intro_id, intro_id),
    ).fetchall()


def list_profiles(conn: Connection, story_id: int, owner_id: int):
    return conn.execute(
        """SELECT id, kind, name, description, image_id FROM (
               SELECT id, 'preset' AS kind, name, description, image_id,
                      0 AS sort_group, position AS sort_position
               FROM story_user_presets WHERE story_id = %s
               UNION ALL
               SELECT id, 'personal', name, description, image_id, 1, 0
               FROM user_profiles WHERE owner_id = %s
           ) AS profiles
           WHERE image_id IS NOT NULL AND name IS NOT NULL
           ORDER BY sort_group, sort_position, id""",
        (story_id, owner_id),
    ).fetchall()


def get_default_play_name(conn: Connection, owner_id: int) -> str | None:
    row = conn.execute(
        "SELECT default_play_name FROM user_play_settings WHERE owner_id = %s", (owner_id,)
    ).fetchone()
    return row["default_play_name"] if row else None


def list_tags(conn: Connection, story_id: int) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(
            """SELECT t.name FROM story_tags st JOIN tags t ON t.id = st.tag_id
               WHERE st.story_id = %s ORDER BY t.name""",
            (story_id,),
        )
    ]


def get_latest_session_id(conn: Connection, story_id: int, owner_id: int) -> int | None:
    row = conn.execute(
        """SELECT id FROM conversation_sessions
           WHERE story_id = %s AND owner_id = %s ORDER BY id DESC LIMIT 1""",
        (story_id, owner_id),
    ).fetchone()
    return row["id"] if row else None


def get_selected_profile(conn: Connection, story_id: int, owner_id: int, kind, profile_id: int):
    if kind == "preset":
        row = conn.execute(
            """SELECT id, name, description, image_id FROM story_user_presets
               WHERE id = %s AND story_id = %s FOR SHARE""",
            (profile_id, story_id),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT id, name, description, image_id FROM user_profiles
               WHERE id = %s AND owner_id = %s FOR SHARE""",
            (profile_id, owner_id),
        ).fetchone()
    return dict(row, kind=kind) if row else None


def get_initial_session_id(
    conn: Connection, story_id: int, owner_id: int, fingerprint: bytes
) -> int | None:
    row = conn.execute(
        """SELECT id FROM conversation_sessions
           WHERE story_id = %s AND owner_id = %s AND initial_request_fingerprint = %s""",
        (story_id, owner_id, fingerprint),
    ).fetchone()
    return row["id"] if row else None


def insert_session(
    conn: Connection,
    story_id: int,
    owner_id: int,
    intro_id: int,
    messages: list,
    fingerprint: bytes,
) -> int:
    return conn.execute(
        """INSERT INTO conversation_sessions
               (story_id, owner_id, source_intro_id, intro_messages_snapshot,
                initial_request_fingerprint)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (story_id, owner_id, intro_id, Jsonb(messages), fingerprint),
    ).fetchone()["id"]


def insert_applied_profile(conn: Connection, session_id: int, profile, name: str):
    from_preset = profile["kind"] == "preset"
    conn.execute(
        """INSERT INTO conversation_user_profiles
               (session_id, source_kind, source_preset_id, source_profile_id,
                name, image_id, description)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            session_id,
            profile["kind"],
            profile["id"] if from_preset else None,
            None if from_preset else profile["id"],
            name,
            profile["image_id"],
            profile["description"],
        ),
    )


def copy_scene_state(conn: Connection, session_id: int, intro_id: int):
    conn.execute(
        """INSERT INTO conversation_scene_states
               (session_id, date_mode, era, year, month, day, reference_event, elapsed_day,
                hour, minute, place)
           SELECT %s, s.date_mode, s.era, NULLIF(s.year_input, '')::int,
                  NULLIF(s.month_input, '')::int, NULLIF(s.day_input, '')::int,
                  s.reference_event, NULLIF(s.elapsed_day_input, '')::int,
                  CASE WHEN s.time_format = '12h'
                       THEN mod(NULLIF(s.hour_input, '')::int, 12)
                            + CASE WHEN s.meridiem = 'pm' THEN 12 ELSE 0 END
                       ELSE NULLIF(s.hour_input, '')::int END,
                  NULLIF(s.minute_input, '')::int, s.place
           FROM intros i LEFT JOIN intro_scene_settings s ON s.intro_id = i.id
           WHERE i.id = %s""",
        (session_id, intro_id),
    )


def copy_character_states(conn: Connection, session_id: int, story_id: int, intro_id: int):
    conn.execute(
        """INSERT INTO conversation_character_states
               (session_id, source_character_id, name_snapshot, clothing, inner_thought)
           SELECT %s, c.id, c.name, s.clothing, s.inner_thought
           FROM characters c LEFT JOIN intro_character_states s
             ON s.character_id = c.id AND s.intro_id = %s
           WHERE c.story_id = %s ORDER BY c.position""",
        (session_id, intro_id, story_id),
    )


def copy_affinities(conn: Connection, session_id: int):
    conn.execute(
        """INSERT INTO conversation_affinities
               (session_id, source_session_character_id, target_kind,
                target_session_character_id, affinity)
           SELECT src.session_id, src.id, a.target_kind, dst.id, NULLIF(a.affinity_input, '')::int
           FROM character_initial_affinities a
           JOIN conversation_character_states src
             ON src.session_id = %s AND src.source_character_id = a.source_character_id
           LEFT JOIN conversation_character_states dst
             ON dst.session_id = src.session_id AND dst.source_character_id = a.target_character_id
           WHERE a.target_kind = 'user' OR dst.id IS NOT NULL""",
        (session_id,),
    )


def copy_relationships(conn: Connection, session_id: int):
    conn.execute(
        """INSERT INTO conversation_relationships
               (session_id, source_kind, source_session_character_id, target_kind,
                target_session_character_id, label)
           SELECT s.id, r.source_kind, src.id, r.target_kind, dst.id, r.label
           FROM conversation_sessions s
           CROSS JOIN character_initial_relationships r
           LEFT JOIN conversation_character_states src
             ON src.session_id = s.id AND src.source_character_id = r.source_character_id
           LEFT JOIN conversation_character_states dst
             ON dst.session_id = s.id AND dst.source_character_id = r.target_character_id
           WHERE s.id = %s AND (r.source_kind = 'user' OR src.id IS NOT NULL)
                            AND (r.target_kind = 'user' OR dst.id IS NOT NULL)""",
        (session_id,),
    )


def get_session(conn: Connection, session_id: int, owner_id: int):
    return conn.execute(
        """SELECT cs.id, s.title AS story_title, cs.intro_messages_snapshot, cs.model,
                  s.deleted_at IS NOT NULL AS read_only, up.name AS play_name
           FROM conversation_sessions cs JOIN stories s ON s.id = cs.story_id
           JOIN conversation_user_profiles up ON up.session_id = cs.id
           WHERE cs.id = %s AND cs.owner_id = %s
             AND (s.deleted_at IS NOT NULL OR s.save_state = 'public'
                  OR (s.owner_id = cs.owner_id AND s.save_state IN ('public', 'private')))""",
        (session_id, owner_id),
    ).fetchone()


def list_session_characters(conn: Connection, session_id: int):
    return conn.execute(
        """SELECT id, name_snapshot AS name FROM conversation_character_states
           WHERE session_id = %s ORDER BY id""",
        (session_id,),
    ).fetchall()
