"""CREATE-US-09 확정 분류 입력 조회와 분류 필요 표시."""

from psycopg.rows import tuple_row


def read_inputs(connection, story_id):
    with connection.cursor(row_factory=tuple_row) as cursor:
        return tuple(
            cursor.execute(
                """SELECT s.world_description,
                          COALESCE(jsonb_agg(jsonb_build_array(c.id, c.description) ORDER BY c.id)
                                   FILTER (WHERE c.id IS NOT NULL), '[]'::jsonb)
                   FROM stories s LEFT JOIN characters c ON c.story_id = s.id
                   WHERE s.id = %s GROUP BY s.id""",
                (story_id,),
            ).fetchone()
        )


def mark_needed(connection, story_id):
    connection.execute(
        """UPDATE stories SET category_classification_needed = true
           WHERE id = %s AND NOT category_classification_needed
             AND save_state IN ('private', 'public') AND deleted_at IS NULL""",
        (story_id,),
    )
