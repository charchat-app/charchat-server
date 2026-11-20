"""임시 저장본(2026-11-20 결정)의 이미지 목록 저장 통합검사.

- 비공개·공개 스토리의 임시 저장은 임시 저장본에만 두고, 이용자가 보는 사진은 그대로다.
- 임시 저장본이 쓰는 사진은 파일 정리가 지우지 않는다.
- 저장하면 반영하고, 연결이 끊긴 예전 사진은 바로 지운다.
"""

from datetime import timedelta

import psycopg
from psycopg.rows import dict_row

from app.features.creation.images import service
from tests.features.creation.images.helpers import create_story, uploaded_id


def url(story_id):
    return f"/api/v1/creation/stories/{story_id}/images"


def put(api, story_id, characters, kind, key):
    body = {
        "story_image_ids": [],
        "characters": [{"character_id": c, "image_ids": ids} for c, ids in characters.items()],
        "kind": kind,
    }
    return api.put(url(story_id), json=body, headers={"Idempotency-Key": key})


def linked(db, character_id):
    rows = db.execute(
        "SELECT image_id FROM character_images WHERE character_id=%s ORDER BY position",
        (character_id,),
    ).fetchall()
    return [row["image_id"] for row in rows]


def exists(db, image_id):
    return db.execute("SELECT 1 FROM images WHERE id=%s", (image_id,)).fetchone() is not None


def private_story(api, db):
    """하린 a, 도윤 b가 연결된 비공개 스토리."""
    story_id, (harin, doyun) = create_story(db)
    a, b = uploaded_id(api), uploaded_id(api)
    assert put(api, story_id, {harin: [a], doyun: [b]}, "save", "seed").status_code == 200
    db.execute("UPDATE stories SET save_state='private' WHERE id=%s", (story_id,))
    return story_id, harin, doyun, a, b


def test_temporary_save_keeps_public_images_and_skips_required_check(api, db):
    story_id, harin, doyun, a, b = private_story(api, db)
    c = uploaded_id(api)

    temporary = put(api, story_id, {harin: [c], doyun: []}, "temporary", "temp")
    # 임시 저장은 필수 이미지 검사를 하지 않는다(도윤 0장도 허용).
    assert temporary.status_code == 200
    data = temporary.json()
    assert data["working_copy_saved_at"] is not None
    assert [i["image_id"] for i in data["characters"][0]["images"]] == [c]
    assert data["characters_missing_images"] == [doyun]
    # 이용자가 보는 사진은 그대로다.
    assert linked(db, harin) == [a]
    assert linked(db, doyun) == [b]
    # 다시 열면 임시 저장본이 나온다.
    reopened = api.get(url(story_id)).json()
    assert reopened["working_copy_saved_at"] is not None
    assert [i["image_id"] for i in reopened["characters"][0]["images"]] == [c]


def test_cleanup_keeps_images_used_only_by_working_copy(api, db, db_url, storage):
    story_id, harin, _, a, _ = private_story(api, db)
    c = uploaded_id(api)
    put(api, story_id, {harin: [c]}, "temporary", "temp")
    assert (
        db.execute("SELECT orphaned_at FROM images WHERE id=%s", (c,)).fetchone()["orphaned_at"]
        is None
    )
    with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as connection:
        result = service.cleanup_unused_images(connection, storage, timedelta(0))
    assert c not in result.deleted
    assert a not in result.deleted
    assert exists(db, c) and exists(db, a)


def test_save_applies_working_copy_and_deletes_replaced_image(api, db):
    story_id, harin, doyun, a, b = private_story(api, db)
    c = uploaded_id(api)
    put(api, story_id, {harin: [c]}, "temporary", "temp")

    saved = put(api, story_id, {harin: [c]}, "save", "commit")
    assert saved.status_code == 200
    assert saved.json()["working_copy_saved_at"] is None
    assert linked(db, harin) == [c]
    assert linked(db, doyun) == [b]
    # 예전 사진은 저장할 때 바로 지운다.
    assert not exists(db, a)
    assert api.get(url(story_id)).json()["working_copy_saved_at"] is None
    assert db.execute("SELECT count(*) AS n FROM story_working_copies").fetchone()["n"] == 0


def test_save_still_requires_image_per_character_and_keeps_working_copy(api, db):
    story_id, harin, doyun, a, b = private_story(api, db)
    put(api, story_id, {doyun: []}, "temporary", "temp")

    rejected = put(api, story_id, {doyun: []}, "save", "commit")
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "CHARACTER_IMAGE_REQUIRED"
    assert linked(db, doyun) == [b]
    assert exists(db, b)
    # 거절돼도 임시 저장본은 남아 있어 계속 고칠 수 있다.
    assert api.get(url(story_id)).json()["working_copy_saved_at"] is not None


def test_discarded_temporary_image_is_released_when_saved_without_it(api, db):
    story_id, harin, _, a, _ = private_story(api, db)
    c = uploaded_id(api)
    put(api, story_id, {harin: [c]}, "temporary", "temp")
    # 임시 저장본에만 있던 사진을 빼고 저장하면 그 사진도 바로 지운다.
    assert put(api, story_id, {harin: [a]}, "save", "keep-original").status_code == 200
    assert linked(db, harin) == [a]
    assert not exists(db, c)
