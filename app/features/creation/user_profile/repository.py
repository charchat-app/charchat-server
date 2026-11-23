"""CREATE-US-05 DB 접근 (story_user_presets). 트랜잭션은 호출하는 쪽이 연다.

커밋·롤백·연결 생성을 하지 않는다. 모든 쓰기는 story_id 범위 안에서만 일어난다.
"""

from typing import Any

from psycopg import Connection

Row = dict[str, Any]


def lock_story(conn: Connection, story_id: int, owner_id: int) -> bool:
    """본인의 삭제되지 않은 스토리 행을 잠그고 존재 여부를 돌려준다.

    같은 스토리의 프리셋을 동시에 교체하지 못하게 한다.
    """
    row = conn.execute(
        "SELECT 1 FROM stories WHERE id = %s AND owner_id = %s AND deleted_at IS NULL FOR UPDATE",
        (story_id, owner_id),
    ).fetchone()
    return row is not None


def list_presets(conn: Connection, story_id: int) -> list[Row]:
    return conn.execute(
        """
        SELECT id, name, image_id, description, position FROM story_user_presets
        WHERE story_id = %s ORDER BY position, id
        """,
        (story_id,),
    ).fetchall()


def find_image_owners(
    conn: Connection, image_ids: list[int], owner_id: int, *, include_gif: bool = False
) -> set[int]:
    """`owner_id`가 소유한 이미지 중 프리셋에 연결할 수 있는 id 집합. images는 쓰지 않는다.

    - 존재하지 않는 id, 남의 이미지, GIF(스토리 이미지 전용, CREATE-US-06 AC05)는 결과에 없다.
    - 소유자 조건은 SQL에서 먼저 걸러, `FOR UPDATE`는 소유 행에만 id 순으로 잡는다.
      남의 이미지 행이 다른 트랜잭션에 잠겨 있어도 기다리지 않는다.
      잠근 행은 연결 변경과 미사용 표시 갱신을 정리 작업·다른 프리셋 저장과 직렬화한다.
    """
    rows = conn.execute(
        """
        SELECT id, mime_type FROM images
        WHERE id = ANY(%s::bigint[]) AND owner_id = %s
        ORDER BY id
        FOR UPDATE
        """,
        (image_ids, owner_id),
    ).fetchall()
    return {r["id"] for r in rows if include_gif or r["mime_type"] != "image/gif"}


def delete_presets_except(conn: Connection, story_id: int, keep_ids: list[int]) -> None:
    """목록에서 빠진 프리셋만 삭제한다.

    적용된 세션 프로필의 출처 id는 FK(ON DELETE SET NULL)가 NULL로 바꾼다.
    """
    conn.execute(
        "DELETE FROM story_user_presets WHERE story_id = %s AND NOT (id = ANY(%s::bigint[]))",
        (story_id, keep_ids),
    )


def update_preset(
    conn: Connection,
    story_id: int,
    preset_id: int,
    name: str | None,
    image_id: int | None,
    description: str | None,
    position: int,
) -> bool:
    """id를 유지한 채 수정한다.

    (story_id, position) UNIQUE는 지연 검사라 한 트랜잭션 안에서 순서를 맞바꿀 수 있다.
    """
    cur = conn.execute(
        """
        UPDATE story_user_presets
        SET name = %s, image_id = %s, description = %s, position = %s
        WHERE id = %s AND story_id = %s
        """,
        (name, image_id, description, position, preset_id, story_id),
    )
    return cur.rowcount == 1


def insert_preset(
    conn: Connection,
    story_id: int,
    name: str | None,
    image_id: int | None,
    description: str | None,
    position: int,
) -> None:
    conn.execute(
        """
        INSERT INTO story_user_presets (story_id, name, image_id, description, position)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (story_id, name, image_id, description, position),
    )
