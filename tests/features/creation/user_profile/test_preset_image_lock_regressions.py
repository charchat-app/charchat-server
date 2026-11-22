"""프리셋 이미지 잠금 회귀 테스트 (리뷰 지적 2건).

1. `lock_preset_images`는 다른 소유자의 이미지가 잠겨 있어도 소유 이미지만 돌려준다.
2. 정리 작업의 `mark_unused_images`·`unmark_used_images`는 편집 트랜잭션이 잠근 행을 건너뛰고
   나머지 대상은 처리하며, 잠금이 풀린 뒤 다시 돌면 건너뛴 행도 맞춘다.

서로 다른 PostgreSQL 연결로 실제 행 잠금을 재현한다. 대기하면 `lock_timeout`으로 실패한다.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.features.creation.images import repository as images_repo
from app.features.creation.user_profile.schemas import AuthorizedStory
from app.features.creation.user_profile.service import lock_preset_images

OWNER_ID = 9001
OTHER_OWNER_ID = 9002
LOCK_TIMEOUT = "200ms"


def _insert_image(db: psycopg.Connection, owner_id: int, key: str, *, orphaned: bool) -> int:
    return db.execute(
        """INSERT INTO images (owner_id, storage_key, mime_type, byte_size, orphaned_at)
           VALUES (%s, %s, 'image/png', 10, CASE WHEN %s THEN clock_timestamp() END)
           RETURNING id""",
        (owner_id, key, orphaned),
    ).fetchone()["id"]


def _insert_story(db: psycopg.Connection, owner_id: int) -> int:
    return db.execute(
        "INSERT INTO stories (owner_id) VALUES (%s) RETURNING id", (owner_id,)
    ).fetchone()["id"]


def _attach_preset(db: psycopg.Connection, story_id: int, image_id: int, position: int) -> None:
    db.execute(
        "INSERT INTO story_user_presets (story_id, image_id, position) VALUES (%s, %s, %s)",
        (story_id, image_id, position),
    )


def _orphaned(db: psycopg.Connection, image_id: int) -> bool:
    row = db.execute("SELECT orphaned_at FROM images WHERE id = %s", (image_id,)).fetchone()
    return row["orphaned_at"] is not None


@contextmanager
def _editor_holding_lock(db_url: str, image_id: int) -> Iterator[None]:
    """편집 트랜잭션이 이미지 행을 `FOR UPDATE`로 잠근 상태를 유지한다."""
    with psycopg.connect(db_url) as editor:
        editor.execute("SELECT id FROM images WHERE id = %s FOR UPDATE", (image_id,))
        try:
            yield
        finally:
            editor.rollback()


@contextmanager
def _short_lock_timeout_tx(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            yield conn


def test_lock_preset_images_skips_foreign_locked_row_without_waiting(db, db_url):
    # CREATE-US-05: 다른 소유자 이미지 잠금 때문에 프리셋 저장이 기다리지 않는다.
    story_id = _insert_story(db, OWNER_ID)
    owned = _insert_image(db, OWNER_ID, "regr/owned.png", orphaned=True)
    foreign = _insert_image(db, OTHER_OWNER_ID, "regr/foreign.png", orphaned=True)
    story = AuthorizedStory(story_id=story_id, owner_id=OWNER_ID)

    with _editor_holding_lock(db_url, foreign):
        with _short_lock_timeout_tx(db_url) as conn:
            locked = lock_preset_images(conn, story, [owned, foreign])

    assert locked == {owned}


def test_mark_unused_images_skips_locked_row_then_reconciles(db, db_url):
    # CREATE-US-06: 미사용 표시 정리는 편집 중 잠긴 행을 건너뛰고 다음 실행에서 맞춘다.
    locked_image = _insert_image(db, OWNER_ID, "regr/mark-locked.png", orphaned=False)
    free_image = _insert_image(db, OWNER_ID, "regr/mark-free.png", orphaned=False)

    with _editor_holding_lock(db_url, locked_image):
        with _short_lock_timeout_tx(db_url) as conn:
            assert images_repo.mark_unused_images(conn) == 1

    assert _orphaned(db, free_image)
    assert not _orphaned(db, locked_image)

    with _short_lock_timeout_tx(db_url) as conn:
        assert images_repo.mark_unused_images(conn) == 1
    assert _orphaned(db, locked_image)


def test_unmark_used_images_skips_locked_row_then_reconciles(db, db_url):
    # CREATE-US-06: 다시 연결된 이미지의 표시 해제도 잠긴 행을 건너뛰고 다음 실행에서 맞춘다.
    story_id = _insert_story(db, OWNER_ID)
    locked_image = _insert_image(db, OWNER_ID, "regr/unmark-locked.png", orphaned=True)
    free_image = _insert_image(db, OWNER_ID, "regr/unmark-free.png", orphaned=True)
    _attach_preset(db, story_id, locked_image, 0)
    _attach_preset(db, story_id, free_image, 1)

    with _editor_holding_lock(db_url, locked_image):
        with _short_lock_timeout_tx(db_url) as conn:
            assert images_repo.unmark_used_images(conn) == 1

    assert not _orphaned(db, free_image)
    assert _orphaned(db, locked_image)

    with _short_lock_timeout_tx(db_url) as conn:
        assert images_repo.unmark_used_images(conn) == 1
    assert not _orphaned(db, locked_image)
