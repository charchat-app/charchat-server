"""CREATE-US-06 AC10·AC11 미사용 이미지 정리의 실제 PostgreSQL 통합검사."""

import threading
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row

from app.features.creation.images import cleanup, service
from app.features.creation.images.storage import LocalImageStorage
from tests.features.creation.images.helpers import OWNER_ID, PNG, create_story

GRACE = timedelta(hours=24)


def add_image(db, storage, *, unused_for_hours=None):
    """파일과 이미지 행을 만든다. unused_for_hours를 주면 그만큼 전부터 미사용인 것으로 둔다."""
    key = storage.save(PNG, "png")
    orphaned = None if unused_for_hours is None else f"{unused_for_hours} hours"
    return db.execute(
        """INSERT INTO images(owner_id, storage_key, mime_type, byte_size, orphaned_at)
           VALUES (%s, %s, 'image/png', %s, clock_timestamp() - %s::interval)
           RETURNING id, storage_key""",
        (OWNER_ID, key, len(PNG), orphaned),
    ).fetchone()


def exists(db, storage, image):
    row = db.execute("SELECT 1 FROM images WHERE id=%s", (image["id"],)).fetchone()
    return row is not None, storage.path_of(image["storage_key"]) is not None


def run(db_url, storage, grace=GRACE):
    with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as connection:
        return service.cleanup_unused_images(connection, storage, grace)


def link_to_character(db, character_id, image_id, position=0):
    db.execute(
        "INSERT INTO character_images(character_id, image_id, position) VALUES (%s,%s,%s)",
        (character_id, image_id, position),
    )


def test_only_images_unused_past_grace_are_deleted_CREATE_US_06_AC11(db, db_url, storage):
    old = add_image(db, storage, unused_for_hours=25)
    fresh = add_image(db, storage, unused_for_hours=1)  # 업로드 직후·저장 전 이미지
    result = run(db_url, storage)
    assert result.deleted == [old["id"]]
    assert exists(db, storage, old) == (False, False)
    assert exists(db, storage, fresh) == (True, True)


def test_linked_image_is_never_deleted_even_if_marked_stale_CREATE_US_06_AC10(db, db_url, storage):
    _, (harin, _) = create_story(db)
    used = add_image(db, storage, unused_for_hours=48)  # 표시가 낡았어도
    link_to_character(db, harin, used["id"])
    result = run(db_url, storage)
    assert result.deleted == []
    assert exists(db, storage, used) == (True, True)
    # 다시 연결된 이미지의 미사용 표시는 지운다.
    assert (
        db.execute("SELECT orphaned_at FROM images WHERE id=%s", (used["id"],)).fetchone()[
            "orphaned_at"
        ]
        is None
    )


def test_image_shared_with_other_target_survives_source_deletion_CREATE_US_06_AC10(
    db, db_url, storage
):
    story_id, (harin, doyun) = create_story(db)
    shared = add_image(db, storage)
    link_to_character(db, harin, shared["id"])
    db.execute(
        "INSERT INTO story_images(story_id, image_id, position) VALUES (%s,%s,0)",
        (story_id, shared["id"]),
    )
    # 원본 캐릭터를 지워도(연결만 CASCADE 삭제) 스토리 쪽 연결이 남아 파일을 지키며 계속 조회된다.
    db.execute("DELETE FROM characters WHERE id=%s", (harin,))
    result = run(db_url, storage, grace=timedelta(0))
    assert shared["id"] not in result.deleted
    assert exists(db, storage, shared) == (True, True)


def test_cascade_unlinked_image_starts_grace_then_is_deleted_CREATE_US_06_AC11(db, db_url, storage):
    _, (harin, _) = create_story(db)
    face = add_image(db, storage)
    link_to_character(db, harin, face["id"])
    db.execute("DELETE FROM characters WHERE id=%s", (harin,))

    first = run(db_url, storage)
    # 연결이 끊긴 것을 이번에 발견했으므로 지금부터 유예가 시작된다(바로 지우지 않음).
    assert first.marked == 1
    assert first.deleted == []
    assert exists(db, storage, face) == (True, True)

    db.execute(
        "UPDATE images SET orphaned_at = clock_timestamp() - interval '25 hours' WHERE id=%s",
        (face["id"],),
    )
    assert run(db_url, storage).deleted == [face["id"]]
    assert exists(db, storage, face) == (False, False)


def test_file_delete_failure_keeps_row_for_retry_CREATE_US_06_AC11(
    db, db_url, storage, monkeypatch
):
    old = add_image(db, storage, unused_for_hours=30)

    def broken_delete(key):
        raise OSError("저장소 삭제 실패")

    monkeypatch.setattr(storage, "delete", broken_delete)
    result = run(db_url, storage)
    assert result.failed == [old["id"]]
    assert result.deleted == []
    assert exists(db, storage, old) == (True, True)

    monkeypatch.undo()
    assert run(db_url, storage).deleted == [old["id"]]
    assert exists(db, storage, old) == (False, False)


def test_image_being_linked_concurrently_is_not_deleted_CREATE_US_06_AC11(db, db_url, storage):
    """저장이 연결하는 중(커밋 전)에 정리가 돌아도 그 이미지를 지우지 않는다."""
    _, (harin, _) = create_story(db)
    old = add_image(db, storage, unused_for_hours=30)
    linked = threading.Event()
    release = threading.Event()

    def saver():
        with psycopg.connect(db_url) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO character_images(character_id, image_id, position) "
                    "VALUES (%s,%s,0)",
                    (harin, old["id"]),
                )
                linked.set()
                release.wait(10)

    thread = threading.Thread(target=saver)
    thread.start()
    assert linked.wait(10)
    during = run(db_url, storage)
    release.set()
    thread.join(10)

    assert old["id"] in during.kept
    assert during.deleted == []
    assert exists(db, storage, old) == (True, True)
    # 저장이 끝난 뒤에는 사용 중으로 확인되어 미사용 표시가 지워진다.
    after = run(db_url, storage)
    assert after.deleted == []
    assert (
        db.execute("SELECT orphaned_at FROM images WHERE id=%s", (old["id"],)).fetchone()[
            "orphaned_at"
        ]
        is None
    )


def test_cli_runs_cleanup_with_configured_storage(db, db_url, tmp_path, monkeypatch, capsys):
    storage = LocalImageStorage(tmp_path / "cli-media")
    old = add_image(db, storage, unused_for_hours=30)
    monkeypatch.setattr(cleanup.settings, "database_url", db_url)
    monkeypatch.setattr(cleanup.settings, "media_root", str(storage.root))
    assert cleanup.main(["--grace-hours", "24"]) == 0
    assert "삭제 1" in capsys.readouterr().out
    assert exists(db, storage, old) == (False, False)
