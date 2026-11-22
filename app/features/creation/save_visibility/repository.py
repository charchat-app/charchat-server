"""7번 공통 저장의 완성 조건 조회와 저장 상태 변경 SQL."""

# TODO(KAN-13): common/ 조회 함수로 교체 (리뷰 server#5)
# 아래 1번(stories·characters·tags) 조회는 다른 기능의 테이블을 직접 읽는다.
# 5번 프리셋은 user_profile 서비스(CREATE-US-05)를 통해 읽고 쓴다.
# common/에 조회 함수가 생기면 그 함수를 호출하고 이 파일의 해당 함수는 지운다.


def read_basic(connection, story_id):
    # TODO(KAN-13): common/ 조회 함수로 교체 (리뷰 server#5) — 1번 stories
    return connection.execute(
        "SELECT title,subtitle,world_description FROM stories WHERE id=%s", (story_id,)
    ).fetchone()


def read_characters(connection, story_id):
    # TODO(KAN-13): common/ 조회 함수로 교체 (리뷰 server#5) — 1번 characters
    return connection.execute(
        "SELECT id,name,description FROM characters WHERE story_id=%s ORDER BY position,id",
        (story_id,),
    ).fetchall()


def read_tag_names(connection, story_id):
    # TODO(KAN-13): common/ 조회 함수로 교체 (리뷰 server#5) — 1번 tags
    # 오류 필드 tags[i]가 기본 정보 GET(story_basic, tags.id 순)과 같은 태그를 가리키게 한다.
    rows = connection.execute(
        """SELECT t.name FROM tags t JOIN story_tags s ON s.tag_id=t.id
           WHERE s.story_id=%s ORDER BY t.id""",
        (story_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def read_rating(connection, story_id):
    """CREATE-US-08 저장본 등급 선택·적용 등급·검토 상태."""
    return connection.execute(
        """SELECT requested_rating, effective_rating, rating_review_status
           FROM stories WHERE id=%s""",
        (story_id,),
    ).fetchone()


def update_requested_rating(connection, story_id, requested_rating):
    """CREATE-US-08-AC01: 제작자의 등급 선택만 바꾼다.

    적용 등급·검토 상태·사유·요청 시각은 이 조각에서 쓰지 않는다(검토 연동은 후속 작업).
    """
    connection.execute(
        "UPDATE stories SET requested_rating=%s WHERE id=%s", (requested_rating, story_id)
    )


def update_save_state(connection, story_id, save_state):
    return connection.execute(
        "UPDATE stories SET save_state=%s WHERE id=%s RETURNING save_state,saved_at",
        (save_state, story_id),
    ).fetchone()
