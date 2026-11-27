"""CREATE-US-01 DB 접근. 트랜잭션은 호출하는 서비스가 연다."""

from typing import Any

from psycopg import Connection

Row = dict[str, Any]


def set_consistent_read(conn: Connection) -> None:
    """검색 결과와 공통 안전 판정(CREATE-US-08-AC15)이 같은 스냅숏을 보게 한다."""
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


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
        SELECT id, save_state, title, saved_at, effective_rating, rating_review_status
        FROM stories
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


def list_characters(conn: Connection, story_id: int) -> list[Row]:
    return conn.execute(
        """
        SELECT id, name, description, position FROM characters
        WHERE story_id = %s ORDER BY position, id
        """,
        (story_id,),
    ).fetchall()


def list_character_ids(conn: Connection, story_id: int) -> set[int]:
    rows = conn.execute("SELECT id FROM characters WHERE story_id = %s", (story_id,)).fetchall()
    return {r["id"] for r in rows}


def delete_characters_except(conn: Connection, story_id: int, keep_ids: list[int]) -> None:
    """목록에서 빠진 캐릭터를 삭제한다.

    얼굴 이미지 연결·발화 말풍선·인트로별 상태·초기 호감도·관계는 스키마의 ON DELETE CASCADE로
    함께 정리된다 (CREATE-US-02-AC15, 03-AC06, 04-AC16).
    """
    conn.execute(
        "DELETE FROM characters WHERE story_id = %s AND NOT (id = ANY(%s::bigint[]))",
        (story_id, keep_ids),
    )


def update_character(
    conn: Connection, character_id: int, name: str | None, description: str | None, position: int
) -> None:
    # (story_id, position) UNIQUE는 지연 검사라 한 트랜잭션 안에서 순서를 맞바꿀 수 있다.
    conn.execute(
        "UPDATE characters SET name = %s, description = %s, position = %s WHERE id = %s",
        (name, description, position, character_id),
    )


def insert_character(
    conn: Connection, story_id: int, name: str | None, description: str | None, position: int
) -> None:
    conn.execute(
        """
        INSERT INTO characters (story_id, name, description, position)
        VALUES (%s, %s, %s, %s)
        """,
        (story_id, name, description, position),
    )


def _escape_like(text: str) -> str:
    """LIKE 패턴의 특수 문자(%, _)와 이스케이프 문자(!)를 글자 그대로 찾도록 이스케이프한다."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


# 태그 비교 키: 띄어쓰기를 모두 빼고 영문 소문자로 바꾼 값.
# 캐릭터 이름 비교(common/names.py)와 같은 규칙이다.
# 저장은 스토리마다 쓴 표기 그대로 하고, 자동검색·검색·사용 수만 이 키로 묶는다.
_TAG_KEY_SQL = "lower(regexp_replace(t.name, '[[:space:]]', '', 'g'))"


def suggest_tags(conn: Connection, key_prefix: str, limit: int) -> list[Row]:
    """비교 키의 앞부분이 일치하는 태그를 공개 스토리 수 내림차순으로 돌려준다 (AC17).

    - 같은 비교 키(예: Romance·romance, 판타지 로맨스·판타지로맨스)는 후보 한 줄로 묶는다.
      대표 표기는 공개 스토리에서 가장 많이 쓴 표기, 같으면 먼저 생긴 표기다.
    - 공개이고 삭제되지 않은 스토리만, 스토리당 한 번 센다. 0개인 키는 후보에서 뺀다.
    - 같은 수일 때는 대표 표기 이름순(미결 사항의 임시 규칙).
    """
    return conn.execute(
        f"""
        WITH used AS (
            SELECT {_TAG_KEY_SQL} AS key, t.id AS tag_id, t.name, st.story_id
            FROM tags t
            JOIN story_tags st ON st.tag_id = t.id
            JOIN stories s ON s.id = st.story_id
            WHERE s.save_state = 'public' AND s.deleted_at IS NULL
        ),
        counted AS (
            SELECT key, count(DISTINCT story_id) AS public_story_count
            FROM used
            WHERE key LIKE %s ESCAPE '!'
            GROUP BY key
        ),
        spelled AS (
            SELECT DISTINCT ON (key) key, name
            FROM used
            GROUP BY key, tag_id, name
            ORDER BY key, count(*) DESC, tag_id
        )
        SELECT sp.name, c.public_story_count
        FROM counted c JOIN spelled sp USING (key)
        ORDER BY c.public_story_count DESC, sp.name
        LIMIT %s
        """,
        (_escape_like(key_prefix) + "%", limit),
    ).fetchall()


def list_public_stories_by_tag(conn: Connection, tag_key: str, limit: int) -> list[Row]:
    """비교 키가 같은 태그가 붙은 공개·미삭제 스토리를 최근 저장 순으로 돌려준다 (AC16).

    'romance'로 찾으면 Romance·romance가 붙은 스토리를 모두 찾는다.
    앞부분 일치가 아니라 키 전체 일치다.
    """
    return conn.execute(
        f"""
        SELECT s.id, s.title, s.subtitle, s.saved_at,
               coalesce(
                   (SELECT array_agg(t2.name ORDER BY t2.id)
                    FROM story_tags st2 JOIN tags t2 ON t2.id = st2.tag_id
                    WHERE st2.story_id = s.id),
                   ARRAY[]::text[]
               ) AS tags
        FROM stories s
        WHERE s.save_state = 'public' AND s.deleted_at IS NULL
          AND EXISTS (
              SELECT 1 FROM story_tags st JOIN tags t ON t.id = st.tag_id
              WHERE st.story_id = s.id AND {_TAG_KEY_SQL} = %s
          )
        ORDER BY s.saved_at DESC, s.id DESC
        LIMIT %s
        """,
        (tag_key, limit),
    ).fetchall()
