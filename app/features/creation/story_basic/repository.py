"""CREATE-US-01 DB 접근. 트랜잭션은 호출하는 서비스가 연다."""

from typing import Any

from psycopg import Connection

Row = dict[str, Any]


def insert_story(
    conn: Connection, owner_id: int, title: str | None, subtitle: str | None, world: str | None
) -> Row:
    return conn.execute(
        """
        INSERT INTO stories (owner_id, title, subtitle, world_description)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        (owner_id, title, subtitle, world),
    ).fetchone()


def find_story(conn: Connection, story_id: int, *, for_update: bool = False) -> Row | None:
    query = "SELECT * FROM stories WHERE id = %s"
    if for_update:
        query += " FOR UPDATE"
    return conn.execute(query, (story_id,)).fetchone()


def list_owned_stories(conn: Connection, owner_id: int) -> list[Row]:
    """삭제되지 않은 본인 스토리를 최근 저장 순으로 돌려준다."""
    return conn.execute(
        """
        SELECT id, save_state, title, saved_at FROM stories
        WHERE owner_id = %s AND deleted_at IS NULL
        ORDER BY saved_at DESC, id DESC
        """,
        (owner_id,),
    ).fetchall()


def update_story_basic(
    conn: Connection, story_id: int, title: str | None, subtitle: str | None, world: str | None
) -> Row:
    # saved_at은 stories_lifecycle_guard 트리거가 갱신한다.
    return conn.execute(
        """
        UPDATE stories SET title = %s, subtitle = %s, world_description = %s
        WHERE id = %s
        RETURNING *
        """,
        (title, subtitle, world, story_id),
    ).fetchone()


def replace_story_tags(conn: Connection, story_id: int, names: list[str]) -> None:
    """스토리의 해시태그 연결을 통째로 바꾼다. 공용 tags 행은 수정하지 않는다."""
    conn.execute("DELETE FROM story_tags WHERE story_id = %s", (story_id,))
    if not names:
        return
    conn.execute(
        "INSERT INTO tags (name) SELECT unnest(%s::text[]) ON CONFLICT (name) DO NOTHING",
        (names,),
    )
    conn.execute(
        """
        INSERT INTO story_tags (story_id, tag_id)
        SELECT %s, id FROM tags WHERE name = ANY(%s::text[])
        """,
        (story_id, names),
    )


def list_story_tags(conn: Connection, story_id: int) -> list[str]:
    # 태그 순서는 요구로 확정되지 않았다. 재조회 결과가 흔들리지 않게 tags.id 순으로만 고정한다.
    rows = conn.execute(
        """
        SELECT t.name FROM story_tags st JOIN tags t ON t.id = st.tag_id
        WHERE st.story_id = %s ORDER BY t.id
        """,
        (story_id,),
    ).fetchall()
    return [r["name"] for r in rows]
