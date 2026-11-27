"""CREATE-US-09 확정 분류 입력 조회와 분류 필요 표시."""

from psycopg.rows import tuple_row

PENDING = """category_classification_needed AND save_state IN ('private', 'public')
             AND deleted_at IS NULL"""
# 최초 분류 대상: 아직 유효 결과가 한 번도 저장되지 않은 스토리(별칭 s 기준).
NO_RESULTS = "NOT EXISTS (SELECT 1 FROM story_categories sc WHERE sc.story_id = s.id)"


def _eligibility(pending_only, first_only):
    clauses = ([f"({PENDING})"] if pending_only or first_only else []) + (
        [NO_RESULTS] if first_only else []
    )
    return "".join(f" AND {clause}" for clause in clauses)


def read_inputs(connection, story_id, *, pending_only=False, first_only=False):
    eligibility = _eligibility(pending_only, first_only)
    with connection.cursor(row_factory=tuple_row) as cursor:
        return cursor.execute(
            f"""SELECT s.world_description,
                       COALESCE(jsonb_agg(jsonb_build_array(c.id, c.description) ORDER BY c.id)
                                FILTER (WHERE c.id IS NOT NULL), '[]'::jsonb)
                FROM stories s LEFT JOIN characters c ON c.story_id = s.id
                WHERE s.id = %s {eligibility} GROUP BY s.id""",
            (story_id,),
        ).fetchone()


def mark_needed(connection, story_id):
    connection.execute(
        """UPDATE stories SET category_classification_needed = true
           WHERE id = %s AND NOT category_classification_needed
             AND save_state IN ('private', 'public') AND deleted_at IS NULL""",
        (story_id,),
    )


def pending_ids(connection):
    # 최초 유효 결과가 없는 스토리는 최초 분류 경로에서 처리한다.
    return [
        row[0]
        for row in connection.execute(
            f"""SELECT s.id FROM stories s WHERE {PENDING}
                AND EXISTS (SELECT 1 FROM story_categories sc WHERE sc.story_id = s.id)
                ORDER BY s.id"""
        )
    ]


def category_codes(connection):
    return {row[0] for row in connection.execute("SELECT code FROM categories")}


def lock_pending(connection, story_id, *, first_only=False):
    eligibility = _eligibility(True, first_only)
    return (
        connection.execute(
            f"SELECT s.id FROM stories s WHERE s.id=%s{eligibility} FOR UPDATE OF s", (story_id,)
        ).fetchone()
        is not None
    )


def save_result(connection, story_id, codes):
    connection.execute("DELETE FROM story_categories WHERE story_id=%s", (story_id,))
    connection.execute(
        "INSERT INTO story_categories (story_id, category_code) SELECT %s, unnest(%s::text[])",
        (story_id, list(codes)),
    )
    connection.execute(
        "UPDATE stories SET category_classification_needed=false WHERE id=%s", (story_id,)
    )
