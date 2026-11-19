"""CREATE-US-02 매개변수 SQL. 표현 이외의 생성 데이터를 수정하지 않는다."""

import psycopg
from psycopg.types.json import Jsonb


def find_story(connection: psycopg.Connection, story_id: int, owner_id: int, *, lock=False):
    sql = """SELECT id, title, save_state, saved_at, deleted_at FROM stories
             WHERE id = %s AND owner_id = %s"""
    if lock:
        sql += " FOR UPDATE"
    return connection.execute(sql, (story_id, owner_id)).fetchone()


def read_expression(connection: psycopg.Connection, story_id: int):
    return connection.execute(
        """SELECT point_of_view, tense, composition, response_length, style_mode,
                  preset_styles, custom_style FROM story_expression_settings WHERE story_id=%s""",
        (story_id,),
    ).fetchone()


def lock_request(connection: psycopg.Connection, owner_id: int, request_id: str):
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"charchat-creation-write:{owner_id}:{request_id}",),
    )


def find_request(connection: psycopg.Connection, owner_id: int, request_id: str):
    return connection.execute(
        """SELECT request_hash, response FROM creation_write_requests
           WHERE owner_id=%s AND request_id=%s""",
        (owner_id, request_id),
    ).fetchone()


def write_expression(connection: psycopg.Connection, story_id: int, fields: dict):
    connection.execute(
        """INSERT INTO story_expression_settings
           (story_id, point_of_view, tense, composition, response_length,
            style_mode, preset_styles, custom_style)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (story_id) DO UPDATE SET
            point_of_view=EXCLUDED.point_of_view, tense=EXCLUDED.tense,
            composition=EXCLUDED.composition, response_length=EXCLUDED.response_length,
            style_mode=EXCLUDED.style_mode, preset_styles=EXCLUDED.preset_styles,
            custom_style=EXCLUDED.custom_style""",
        (
            story_id,
            fields["point_of_view"],
            fields["tense"],
            fields["composition"],
            fields["response_length"],
            fields["style_mode"],
            fields["preset_styles"],
            fields["custom_style"],
        ),
    )
    return touch_saved_at(connection, story_id)


def touch_saved_at(connection: psycopg.Connection, story_id: int):
    """표현·퓨샷 저장이 공유하는 스토리 마지막 저장 시각 갱신."""
    return connection.execute(
        "UPDATE stories SET saved_at=clock_timestamp() WHERE id=%s RETURNING saved_at",
        (story_id,),
    ).fetchone()["saved_at"]


def remember_request(
    connection: psycopg.Connection,
    owner_id: int,
    request_id: str,
    story_id: int,
    request_hash: str,
    response: dict,
):
    connection.execute(
        """INSERT INTO creation_write_requests
           (owner_id, request_id, story_id, request_hash, response) VALUES (%s,%s,%s,%s,%s)""",
        (owner_id, request_id, story_id, request_hash, Jsonb(response)),
    )
