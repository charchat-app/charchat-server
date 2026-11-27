"""CREATE-US-08-AC02·AC03 × CREATE-US-07: 검토 메타데이터만 바뀐 UPDATE의 saved_at 유지.

`stories_lifecycle_guard`는 적용 등급·검토 상태·사유·요청 표만 실제로 바뀐 UPDATE에서 saved_at을
그대로 둔다. 같은 값 편집 touch(아무것도 바뀌지 않는 UPDATE)·제작자 선택(requested_rating)·다른
필드가 함께 바뀐 UPDATE는 예전처럼 saved_at을 새로 쓴다. 소유자·상태 전환·삭제 보호는 그대로다.
"""

from pathlib import Path

import psycopg
import pytest

from tests.features.creation.speech_style.conftest import OWNER_ID, create_story

MIGRATIONS = Path(__file__).resolve().parents[4] / "app/db/migrations"
MIGRATION = MIGRATIONS / "create_us08_review_saved_at.sql"
DIGEST_MIGRATION = MIGRATIONS / "create_us08_review_pass_digest.sql"
TARGETS_MIGRATION = MIGRATIONS / "create_us08_review_targets.sql"

# 이 수정 전 함수(saved_at을 늘 새로 쓴다). 마이그레이션이 기존 DB에서 고치는지 확인용.
LEGACY_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_story_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Stories must be soft deleted and retained indefinitely'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.deleted_at IS NOT NULL THEN
        RAISE EXCEPTION 'A deleted story cannot be restored or edited'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
        RAISE EXCEPTION 'Story ownership cannot be reassigned by editing'
            USING ERRCODE = '23514';
    END IF;
    IF (OLD.save_state IN ('private', 'public') AND NEW.save_state = 'draft')
       OR (OLD.save_state = 'public' AND NEW.save_state = 'private') THEN
        RAISE EXCEPTION 'This story save-state transition is not allowed'
            USING ERRCODE = '23514';
    END IF;
    NEW.saved_at := clock_timestamp();
    RETURN NEW;
END;
$$;
"""

REVIEW_ONLY = [
    "rating_review_status='reviewing', rating_requested_at=clock_timestamp()",
    "effective_rating='UNSAFE', rating_review_status='completed'",
    "rating_review_reason='ineligible:underage_stated', rating_review_status='failed'",
    # CREATE-US-08-AC14 통과 지문도 검토 메타데이터다.
    f"rating_pass_digest='v1:fake-eval.v1:{'a' * 64}', rating_review_status='completed'",
    # CREATE-US-08-AC09 사유 대상만 바뀐 UPDATE도 검토 메타데이터다.
    """rating_review_targets='{"v": 1, "targets": []}'::jsonb""",
]

ADVANCES = [
    # 같은 값 편집 touch(아무것도 바뀌지 않음)는 예전처럼 저장 시각을 새로 쓴다.
    "title=title",
    "rating_review_status=rating_review_status",
    "effective_rating=effective_rating, rating_review_reason=rating_review_reason",
    # 제작자 선택은 검토 메타데이터가 아니다.
    "requested_rating='UNSAFE'",
    "requested_rating='UNSAFE', rating_review_status='reviewing'",
    # 다른 필드가 함께 바뀌면 저장본 편집이다.
    "title='새 제목', rating_review_status='reviewing'",
    "world_description='새 세계관', effective_rating='UNSAFE'",
    "save_state='private', rating_review_status='reviewing'",
]


def saved_at(db, story):
    return db.execute("SELECT saved_at FROM stories WHERE id=%s", (story["id"],)).fetchone()[
        "saved_at"
    ]


def update(db, story, assignments):
    db.execute(f"UPDATE stories SET {assignments} WHERE id=%s", (story["id"],))


@pytest.mark.parametrize("assignments", REVIEW_ONLY)
def test_review_metadata_only_change_keeps_saved_at_CREATE_US_08_AC03(db, assignments):
    story = create_story(db)
    before = saved_at(db, story)
    update(db, story, assignments)
    assert saved_at(db, story) == before
    # 직접 saved_at을 써도 검토 메타데이터만 바뀌면 서버 저장 시각을 지킨다.
    update(db, story, "rating_requested_at=clock_timestamp(), saved_at=now() + interval '1 day'")
    assert saved_at(db, story) == before


@pytest.mark.parametrize("assignments", ADVANCES)
def test_touch_and_mixed_changes_still_advance_saved_at_CREATE_US_07(db, assignments):
    story = create_story(db)
    before = saved_at(db, story)
    update(db, story, assignments)
    assert saved_at(db, story) > before


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE stories SET owner_id=owner_id + 1, rating_review_status='reviewing' WHERE id=%s",
        "UPDATE stories SET save_state='draft', rating_review_status='reviewing' WHERE id=%s",
        "DELETE FROM stories WHERE id=%s",
    ],
    ids=["owner", "transition", "delete"],
)
def test_guards_still_run_with_review_metadata_CREATE_US_07(db, statement):
    story = create_story(db)
    db.execute("UPDATE stories SET save_state='private' WHERE id=%s", (story["id"],))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(statement, (story["id"],))


def test_deleted_story_rejects_review_metadata_write_CREATE_US_07(db):
    story = create_story(db)
    db.execute("UPDATE stories SET deleted_at=clock_timestamp() WHERE id=%s", (story["id"],))
    with pytest.raises(psycopg.errors.CheckViolation):
        update(db, story, "rating_review_status='completed'")
    assert db.execute("SELECT owner_id FROM stories WHERE id=%s", (story["id"],)).fetchone() == {
        "owner_id": OWNER_ID
    }


def function_definition(db):
    return db.execute(
        "SELECT pg_get_functiondef('public.enforce_story_lifecycle()'::regprocedure) AS d"
    ).fetchone()["d"]


def test_migration_matches_schema_and_reruns_CREATE_US_08_AC03(db):
    expected = function_definition(db)
    migration = MIGRATION.read_text(encoding="utf-8")
    try:
        db.execute(LEGACY_FUNCTION)
        story = create_story(db)
        before = saved_at(db, story)
        update(db, story, REVIEW_ONLY[0])
        assert saved_at(db, story) > before  # 기존 DB(수정 전 함수)는 거짓으로 움직인다.

        db.execute(migration)
        db.execute(migration)  # 재실행해도 같은 결과
        assert function_definition(db) == expected
        before = saved_at(db, story)
        update(db, story, "rating_review_status='completed'")
        assert saved_at(db, story) == before
    finally:
        # 다른 테스트를 위해 스키마 정의로 되돌린다(공유 테스트 DB).
        db.execute(expected)
    assert function_definition(db) == expected


def test_migration_refuses_when_guard_trigger_is_missing_CREATE_US_08_AC03(db):
    migration = MIGRATION.read_text(encoding="utf-8")
    expected = function_definition(db)
    db.execute("ALTER TABLE stories DISABLE TRIGGER stories_lifecycle_guard")
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(migration)
        db.execute("ROLLBACK")
        # 실패하면 함수도 바꾸지 않는다(한 트랜잭션).
        assert function_definition(db) == expected
    finally:
        db.execute("ALTER TABLE stories ENABLE TRIGGER stories_lifecycle_guard")


# --- CREATE-US-08-AC14 통과 지문 컬럼 마이그레이션(배포 순서·재실행) ---

DIGEST_CHECK = "stories_rating_pass_digest_check"


def digest_check(db):
    found = db.execute(
        """SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
           WHERE conrelid='public.stories'::regclass AND conname=%s""",
        (DIGEST_CHECK,),
    ).fetchone()
    return found and found["d"]


@pytest.mark.parametrize(
    "order",
    [
        (DIGEST_MIGRATION, MIGRATION, DIGEST_MIGRATION),
        (MIGRATION, DIGEST_MIGRATION, MIGRATION),
        (DIGEST_MIGRATION, DIGEST_MIGRATION),
    ],
    ids=["digest-first", "saved-at-first", "digest-replay"],
)
def test_digest_migration_any_order_matches_schema_and_keeps_saved_at_CREATE_US_08_AC14(db, order):
    expected_function, expected_check = function_definition(db), digest_check(db)
    try:
        # 마이그레이션 전 기존 DB: 지문 컬럼이 없고 함수는 수정 전이다.
        db.execute(f"ALTER TABLE stories DROP CONSTRAINT {DIGEST_CHECK}")
        db.execute("ALTER TABLE stories DROP COLUMN rating_pass_digest")
        db.execute(LEGACY_FUNCTION)
        for migration in order:
            db.execute(migration.read_text(encoding="utf-8"))
        assert function_definition(db) == expected_function
        assert digest_check(db) == expected_check
        story = create_story(db)
        before = saved_at(db, story)
        update(db, story, f"rating_pass_digest='v1:fake-eval.v1:{'b' * 64}'")
        assert saved_at(db, story) == before
        with pytest.raises(psycopg.errors.CheckViolation):
            update(db, story, "rating_pass_digest='원문이 들어간 값'")
    finally:
        db.execute(DIGEST_MIGRATION.read_text(encoding="utf-8"))
        db.execute(expected_function)
    assert function_definition(db) == expected_function
    assert digest_check(db) == expected_check


def test_digest_migration_refuses_unexpected_check_CREATE_US_08_AC14(db):
    expected_function, expected_check = function_definition(db), digest_check(db)
    db.execute(f"ALTER TABLE stories DROP CONSTRAINT {DIGEST_CHECK}")
    db.execute(
        f"ALTER TABLE stories ADD CONSTRAINT {DIGEST_CHECK} CHECK (rating_pass_digest <> '')"
    )
    db.execute(LEGACY_FUNCTION)
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(DIGEST_MIGRATION.read_text(encoding="utf-8"))
        db.execute("ROLLBACK")
        # 실패하면 함수도 바꾸지 않는다(한 트랜잭션).
        assert function_definition(db) != expected_function
    finally:
        db.execute(f"ALTER TABLE stories DROP CONSTRAINT {DIGEST_CHECK}")
        db.execute(DIGEST_MIGRATION.read_text(encoding="utf-8"))
        db.execute(expected_function)
    assert digest_check(db) == expected_check


# --- CREATE-US-08-AC09 사유 대상 컬럼 마이그레이션(정방향·재실행) ---

TARGETS_CHECK = "stories_rating_review_targets_check"


def targets_snapshot(db):
    """대상 컬럼 정의와 제약 정의(없으면 None)."""
    column = db.execute(
        """SELECT data_type, is_nullable, column_default FROM information_schema.columns
           WHERE table_schema='public' AND table_name='stories'
             AND column_name='rating_review_targets'"""
    ).fetchone()
    check = db.execute(
        """SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
           WHERE conrelid='public.stories'::regclass AND conname=%s""",
        (TARGETS_CHECK,),
    ).fetchone()
    return column, check and check["d"]


@pytest.mark.parametrize(
    "order",
    [
        (TARGETS_MIGRATION,),
        (TARGETS_MIGRATION, TARGETS_MIGRATION),
        (MIGRATION, TARGETS_MIGRATION, DIGEST_MIGRATION),
        (TARGETS_MIGRATION, MIGRATION, TARGETS_MIGRATION),
    ],
    ids=["forward", "replay", "saved-at-first", "targets-first-replay"],
)
def test_targets_migration_forward_and_rerun_match_schema_CREATE_US_08_AC09(db, order):
    expected_function, expected_targets = function_definition(db), targets_snapshot(db)
    assert expected_targets[0] is not None and expected_targets[1] is not None
    try:
        # 마이그레이션 전 기존 DB: 대상 컬럼이 없고 함수는 수정 전이다.
        db.execute(f"ALTER TABLE stories DROP CONSTRAINT {TARGETS_CHECK}")
        db.execute("ALTER TABLE stories DROP COLUMN rating_review_targets")
        db.execute(LEGACY_FUNCTION)
        assert targets_snapshot(db) == (None, None)
        for migration in order:
            db.execute(migration.read_text(encoding="utf-8"))
        assert function_definition(db) == expected_function
        assert targets_snapshot(db) == expected_targets
        story = create_story(db)
        before = saved_at(db, story)
        update(db, story, """rating_review_targets='{"v": 1, "targets": []}'::jsonb""")
        assert saved_at(db, story) == before  # 대상만 바뀌면 저장 시각을 지킨다.
        with pytest.raises(psycopg.errors.CheckViolation):
            update(db, story, "rating_review_targets='[]'::jsonb")
    finally:
        if db.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            db.execute("ROLLBACK")
        db.execute(TARGETS_MIGRATION.read_text(encoding="utf-8"))
        db.execute(expected_function)
    assert function_definition(db) == expected_function
    assert targets_snapshot(db) == expected_targets


# --- CREATE-US-08-AC14 이전 버전 작성기 거부 완료 방어 ---

PASS_A = f"v1:fake-eval.v1:{'a' * 64}"
# 기준 커밋(fcc8a9e)의 결과 쓰기와 같은 SET 목록(지문 컬럼을 모른다).
LEGACY_REJECT = (
    "effective_rating='SAFE', rating_review_status='completed',"
    " rating_review_reason='ineligible:underage_stated'"
)


def reviewing_with_pass(db, story):
    update(
        db,
        story,
        f"requested_rating='UNSAFE', effective_rating='UNSAFE', rating_review_status='reviewing',"
        f" rating_requested_at=clock_timestamp(), rating_pass_digest='{PASS_A}'",
    )


def digest(db, story):
    return db.execute(
        "SELECT rating_pass_digest FROM stories WHERE id=%s", (story["id"],)
    ).fetchone()["rating_pass_digest"]


def test_legacy_writer_rejection_invalidates_kept_digest_CREATE_US_08_AC14(db):
    story = create_story(db)
    reviewing_with_pass(db, story)
    before = saved_at(db, story)
    update(db, story, LEGACY_REJECT)
    assert digest(db, story) is None
    assert saved_at(db, story) == before  # 검토 메타데이터만 바뀌었다.


def test_digest_aware_writer_keeps_older_pass_on_other_content_rejection_CREATE_US_08_AC14(db):
    """지문을 아는 작성기(트랜잭션 표시)는 다른 내용 거부 뒤에도 예전 통과 A를 둔다."""
    story = create_story(db)
    reviewing_with_pass(db, story)
    with db.transaction():
        db.execute("SELECT set_config('charchat.rating_pass_digest_writer', 'v1', true)")
        update(db, story, f"{LEGACY_REJECT}, rating_pass_digest='{PASS_A}'")
    assert digest(db, story) == PASS_A
    # 표시는 트랜잭션 밖으로 새지 않는다: 다음 이전 버전 거부는 다시 비운다.
    reviewing_with_pass(db, story)
    update(db, story, LEGACY_REJECT)
    assert digest(db, story) is None


@pytest.mark.parametrize(
    "assignments",
    [
        # 실패·적격 완료·SAFE 선택은 거부가 아니다(지문을 그대로 둔다).
        "rating_review_status='failed', rating_review_reason='unavailable:evaluator_failure'",
        "effective_rating='UNSAFE', rating_review_status='completed'",
        "requested_rating='SAFE', effective_rating='SAFE', rating_review_status='not_reviewed'",
    ],
)
def test_legacy_non_rejection_keeps_digest_CREATE_US_08_AC14(db, assignments):
    story = create_story(db)
    reviewing_with_pass(db, story)
    update(db, story, assignments)
    assert digest(db, story) == PASS_A
