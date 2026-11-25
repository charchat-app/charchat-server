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
    """CREATE-US-08 저장본 등급 선택·적용 등급·검토 상태·사유 코드."""
    return connection.execute(
        """SELECT requested_rating, effective_rating, rating_review_status, rating_review_reason
           FROM stories WHERE id=%s""",
        (story_id,),
    ).fetchone()


# CREATE-US-08-AC02·AC03·AC09~AC11 검토 결과 저장(조정기에 결과 보관소를 줄 때만 쓴다).
# 요청 표는 rating_requested_at이다. 스토리 잠금 안에서 앞선 값보다 늘 크게 새로 쓴다.
_NEW_TICKET = "GREATEST(clock_timestamp(), rating_requested_at + interval '1 microsecond')"


def mark_review_pending(connection, story_id):
    """검토 대기로 바꾸고 새 요청 표를 돌려준다. 적용 등급은 그대로 둔다(재검토 중 유지)."""
    return connection.execute(
        f"""UPDATE stories SET rating_review_status='reviewing', rating_review_reason=NULL,
                rating_requested_at={_NEW_TICKET}
            WHERE id=%s RETURNING rating_requested_at""",
        (story_id,),
    ).fetchone()["rating_requested_at"]


def mark_review_failed(connection, story_id, reason):
    """검토를 넘기지 못했다. 적용 등급은 그대로 두고 새 요청 표로 실패를 기록한다."""
    connection.execute(
        f"""UPDATE stories SET rating_review_status='failed', rating_review_reason=%s,
                rating_requested_at={_NEW_TICKET}
            WHERE id=%s""",
        (reason, story_id),
    )


def mark_rating_safe(connection, story_id):
    """SAFE 확정 저장. 적용 등급을 SAFE로, 검토 상태를 not_reviewed로 둬 대기 중인 검토를 무효로
    한다(결과 쓰기는 UNSAFE 선택·reviewing일 때만 된다).

    요청 표는 마지막 검토 요청 시각이므로 지우지 않는다. 지우면 다음 요청 표가 앞선 값보다 크다는
    하한이 사라져, 시계가 되돌아갈 때 늦게 끝난 앞선 검토가 같은 요청 표를 다시 쓸 수 있다.

    이미 그 값이면 쓰지 않는다. 같은 값 UPDATE는 트리거가 편집 touch로 보고 saved_at을 새로 써
    이미 만든 응답 saved_at과 어긋나게 한다(검토 메타데이터만 실제로 바뀌면 saved_at은 그대로다).
    """
    connection.execute(
        """UPDATE stories SET effective_rating='SAFE', rating_review_status='not_reviewed',
               rating_review_reason=NULL
           WHERE id=%s
             AND (effective_rating, rating_review_status, rating_review_reason)
                 IS DISTINCT FROM ('SAFE', 'not_reviewed', NULL)""",
        (story_id,),
    )


def lock_review_target(connection, story_id):
    """검토 결과를 쓰기 전에 스토리를 잠그고 요청 표·상태를 읽는다."""
    return connection.execute(
        """SELECT requested_rating, rating_review_status, rating_requested_at, deleted_at
           FROM stories WHERE id=%s FOR UPDATE""",
        (story_id,),
    ).fetchone()


def apply_review_result(connection, story_id, ticket, effective_rating, status, reason) -> bool:
    """요청 표가 그대로이고 UNSAFE 선택·검토 중·삭제되지 않았을 때만 결과를 쓴다.

    `effective_rating`이 None이면 적용 등급을 그대로 둔다.
    """
    row = connection.execute(
        """UPDATE stories
           SET effective_rating=COALESCE(%s, effective_rating),
               rating_review_status=%s, rating_review_reason=%s
           WHERE id=%s AND rating_requested_at=%s AND requested_rating='UNSAFE'
             AND rating_review_status='reviewing' AND deleted_at IS NULL
           RETURNING id""",
        (effective_rating, status, reason, story_id, ticket),
    ).fetchone()
    return row is not None


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
