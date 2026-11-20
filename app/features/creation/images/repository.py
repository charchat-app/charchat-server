"""CREATE-US-06 이미지·연결 SQL. 스토리·캐릭터의 다른 설정은 수정하지 않는다."""

import psycopg

# 이미지 사용 여부를 판단하는 연결 테이블 전체(schema.sql CREATE-US-06 주석 기준).
# 하나라도 남아 있으면 사용 중이며 파일을 지우지 않는다(AC10).
_REFERENCE_SQL = """
    EXISTS (SELECT 1 FROM story_images r WHERE r.image_id = i.id)
    OR EXISTS (SELECT 1 FROM character_images r WHERE r.image_id = i.id)
    OR EXISTS (SELECT 1 FROM story_user_presets r WHERE r.image_id = i.id)
    OR EXISTS (SELECT 1 FROM user_profiles r WHERE r.image_id = i.id)
    OR EXISTS (SELECT 1 FROM conversation_user_profiles r WHERE r.image_id = i.id)
    OR EXISTS (SELECT 1 FROM conversation_image_references r WHERE r.image_id = i.id)
"""


def insert_image(
    connection: psycopg.Connection, owner_id: int, storage_key: str, mime_type: str, size: int
):
    # 업로드 직후에는 어디에도 연결되지 않았으므로 미사용 시작 시각을 바로 기록한다.
    return connection.execute(
        """INSERT INTO images (owner_id, storage_key, mime_type, byte_size, orphaned_at)
           VALUES (%s, %s, %s, %s, clock_timestamp())
           RETURNING id, storage_key, mime_type, byte_size""",
        (owner_id, storage_key, mime_type, size),
    ).fetchone()


def find_owned_images(connection: psycopg.Connection, owner_id: int, image_ids: list[int]):
    if not image_ids:
        return []
    return connection.execute(
        """SELECT id, storage_key, mime_type, byte_size FROM images
           WHERE owner_id = %s AND id = ANY(%s)
           FOR SHARE""",
        (owner_id, image_ids),
    ).fetchall()


def list_characters(connection: psycopg.Connection, story_id: int):
    return connection.execute(
        "SELECT id, name, position FROM characters WHERE story_id = %s ORDER BY position",
        (story_id,),
    ).fetchall()


def read_story_images(connection: psycopg.Connection, story_id: int):
    return connection.execute(
        """SELECT i.id, i.storage_key, i.mime_type, i.byte_size
           FROM story_images s JOIN images i ON i.id = s.image_id
           WHERE s.story_id = %s ORDER BY s.position""",
        (story_id,),
    ).fetchall()


def read_character_images(connection: psycopg.Connection, story_id: int):
    return connection.execute(
        """SELECT c.id AS character_id, i.id, i.storage_key, i.mime_type, i.byte_size
           FROM characters c
           JOIN character_images ci ON ci.character_id = c.id
           JOIN images i ON i.id = ci.image_id
           WHERE c.story_id = %s ORDER BY c.position, ci.position""",
        (story_id,),
    ).fetchall()


def characters_without_images(connection: psycopg.Connection, story_id: int) -> list[int]:
    rows = connection.execute(
        """SELECT c.id FROM characters c
           WHERE c.story_id = %s
             AND NOT EXISTS (SELECT 1 FROM character_images ci WHERE ci.character_id = c.id)
           ORDER BY c.position""",
        (story_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def linked_image_ids(connection: psycopg.Connection, story_id: int, character_ids: list[int]):
    rows = connection.execute(
        """SELECT image_id FROM story_images WHERE story_id = %s
           UNION SELECT image_id FROM character_images WHERE character_id = ANY(%s)""",
        (story_id, character_ids),
    ).fetchall()
    return {row["image_id"] for row in rows}


def replace_story_images(connection: psycopg.Connection, story_id: int, image_ids: list[int]):
    connection.execute("DELETE FROM story_images WHERE story_id = %s", (story_id,))
    for position, image_id in enumerate(image_ids):
        connection.execute(
            "INSERT INTO story_images (story_id, image_id, position) VALUES (%s, %s, %s)",
            (story_id, image_id, position),
        )


def replace_character_images(
    connection: psycopg.Connection, character_id: int, image_ids: list[int]
):
    connection.execute("DELETE FROM character_images WHERE character_id = %s", (character_id,))
    for position, image_id in enumerate(image_ids):
        connection.execute(
            "INSERT INTO character_images (character_id, image_id, position) VALUES (%s, %s, %s)",
            (character_id, image_id, position),
        )


def refresh_orphaned(connection: psycopg.Connection, image_ids: set[int]):
    """연결이 바뀐 이미지의 미사용 시작 시각을 다시 계산한다.

    연결이 하나라도 있으면 NULL, 없으면 처음 미사용이 된 시각을 유지하거나 새로 기록한다.
    실제 파일 정리는 정리 작업이 삭제 직전에 연결을 다시 확인한 뒤 수행한다(AC11).
    """
    if not image_ids:
        return
    connection.execute(
        f"""UPDATE images i SET orphaned_at = CASE
                WHEN {_REFERENCE_SQL} THEN NULL
                ELSE COALESCE(i.orphaned_at, clock_timestamp())
            END
            WHERE i.id = ANY(%s)""",
        (list(image_ids),),
    )


def mark_unused_images(connection: psycopg.Connection) -> int:
    """연결이 없는데 미사용 시각이 비어 있는 이미지에 시각을 기록한다.

    캐릭터 삭제(CASCADE)나 다른 기능의 연결 해제처럼 이 기능을 거치지 않은 변경도 여기서 잡힌다.
    """
    return connection.execute(
        f"""UPDATE images i SET orphaned_at = clock_timestamp()
            WHERE i.orphaned_at IS NULL AND NOT ({_REFERENCE_SQL})"""
    ).rowcount


def unmark_used_images(connection: psycopg.Connection) -> int:
    """미사용으로 표시됐지만 다시 연결된 이미지의 표시를 지운다."""
    return connection.execute(
        f"""UPDATE images i SET orphaned_at = NULL
            WHERE i.orphaned_at IS NOT NULL AND ({_REFERENCE_SQL})"""
    ).rowcount


def expired_image_ids(connection: psycopg.Connection, grace_seconds: float) -> list[int]:
    rows = connection.execute(
        """SELECT id FROM images
           WHERE orphaned_at < clock_timestamp() - make_interval(secs => %s)
           ORDER BY id""",
        (grace_seconds,),
    ).fetchall()
    return [row["id"] for row in rows]


def lock_expired_image(connection: psycopg.Connection, image_id: int, grace_seconds: float):
    """정리할 이미지 행을 잠근다. 다른 저장이 연결 중(잠금 보유)이면 기다리지 않고 건너뛴다."""
    return connection.execute(
        """SELECT id, storage_key FROM images
           WHERE id = %s AND orphaned_at < clock_timestamp() - make_interval(secs => %s)
           FOR UPDATE SKIP LOCKED""",
        (image_id, grace_seconds),
    ).fetchone()


def is_image_used(connection: psycopg.Connection, image_id: int) -> bool:
    return connection.execute(
        f"SELECT ({_REFERENCE_SQL}) AS used FROM images i WHERE i.id = %s", (image_id,)
    ).fetchone()["used"]


def clear_orphaned(connection: psycopg.Connection, image_id: int) -> None:
    connection.execute("UPDATE images SET orphaned_at = NULL WHERE id = %s", (image_id,))


def delete_image_row(connection: psycopg.Connection, image_id: int) -> None:
    connection.execute("DELETE FROM images WHERE id = %s", (image_id,))


def touch_saved_at(connection: psycopg.Connection, story_id: int):
    return connection.execute(
        "UPDATE stories SET saved_at = clock_timestamp() WHERE id = %s RETURNING saved_at",
        (story_id,),
    ).fetchone()["saved_at"]


def find_file_image(connection: psycopg.Connection, storage_key: str):
    return connection.execute(
        "SELECT id, mime_type FROM images WHERE storage_key = %s", (storage_key,)
    ).fetchone()
