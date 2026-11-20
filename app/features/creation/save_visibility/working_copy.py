"""임시 저장본(스토리마다 1개). 비공개·공개 스토리를 고치다 임시 저장한 내용을 둔다.

공개 상태(미등록·비공개·공개)와 저장 상태(임시 저장·저장)를 나눈 결정(2026-11-20)에 따른다.
- 임시 저장: 이 표에만 쓴다. 이용자가 보는 본 테이블은 그대로다.
- 저장: 본 테이블에 반영하고 그 영역의 임시 저장본을 비운다.
미등록 스토리는 아무도 보지 않으므로 임시 저장도 본 테이블에 바로 쓰고 이 표를 쓰지 않는다.
"""

import psycopg
from psycopg.types.json import Jsonb


def read(connection: psycopg.Connection, story_id: int):
    return connection.execute(
        """SELECT creation, creation_saved_at, images, images_saved_at
           FROM story_working_copies WHERE story_id = %s""",
        (story_id,),
    ).fetchone()


def write_creation(connection: psycopg.Connection, story_id: int, document: dict):
    """2~4번 공통 저장 문서를 임시 저장본에 둔다. 임시 저장 시각을 돌려준다."""
    return connection.execute(
        """INSERT INTO story_working_copies(story_id, creation, creation_saved_at)
           VALUES (%s, %s, clock_timestamp())
           ON CONFLICT (story_id) DO UPDATE
             SET creation = EXCLUDED.creation, creation_saved_at = EXCLUDED.creation_saved_at
           RETURNING creation_saved_at""",
        (story_id, Jsonb(document)),
    ).fetchone()["creation_saved_at"]


def write_images(
    connection: psycopg.Connection, story_id: int, document: dict, image_ids: set[int]
) -> set[int]:
    """이미지 목록 문서를 임시 저장본에 둔다. 이전 임시 저장본이 쓰던 이미지 ID를 돌려준다."""
    connection.execute(
        """INSERT INTO story_working_copies(story_id, images, images_saved_at)
           VALUES (%s, %s, clock_timestamp())
           ON CONFLICT (story_id) DO UPDATE
             SET images = EXCLUDED.images, images_saved_at = EXCLUDED.images_saved_at""",
        (story_id, Jsonb(document)),
    )
    previous = _replace_image_links(connection, story_id, image_ids)
    return previous


def clear_creation(connection: psycopg.Connection, story_id: int) -> None:
    connection.execute(
        """UPDATE story_working_copies SET creation = NULL, creation_saved_at = NULL
           WHERE story_id = %s""",
        (story_id,),
    )
    _drop_if_empty(connection, story_id)


def clear_images(connection: psycopg.Connection, story_id: int) -> set[int]:
    """이미지 임시 저장본을 비운다. 그 임시 저장본이 쓰던 이미지 ID를 돌려준다."""
    previous = _replace_image_links(connection, story_id, set())
    connection.execute(
        """UPDATE story_working_copies SET images = NULL, images_saved_at = NULL
           WHERE story_id = %s""",
        (story_id,),
    )
    _drop_if_empty(connection, story_id)
    return previous


def _replace_image_links(connection, story_id: int, image_ids: set[int]) -> set[int]:
    rows = connection.execute(
        "DELETE FROM story_working_copy_images WHERE story_id = %s RETURNING image_id",
        (story_id,),
    ).fetchall()
    if image_ids:
        connection.execute(
            """INSERT INTO story_working_copy_images(story_id, image_id)
               SELECT %s, unnest(%s::bigint[])""",
            (story_id, sorted(image_ids)),
        )
    return {row["image_id"] for row in rows}


def _drop_if_empty(connection, story_id: int) -> None:
    connection.execute(
        """DELETE FROM story_working_copies
           WHERE story_id = %s AND creation IS NULL AND images IS NULL""",
        (story_id,),
    )
