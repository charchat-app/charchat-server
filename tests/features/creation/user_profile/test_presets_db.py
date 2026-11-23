"""CREATE-US-05 프리셋 교체의 이미지 연결 규칙 — 실제 PostgreSQL 통합검사.

`tests/conftest.py`의 `db` 픽스처를 쓴다. `TEST_DATABASE_URL`(폐기 가능한 전용 테스트 DB)이
없으면 건너뛴다. 건너뛴 결과는 검증 완료가 아니다.

여기서 고정하는 계약은 이미지 기능(CREATE-US-06)이 이미 지키는 규칙을 프리셋도 따르는 것이다.
- GIF는 스토리 이미지에만 쓸 수 있다 (images/service.py `IMAGE_GIF_NOT_ALLOWED`, CREATE-US-06 AC05).
- 연결이 바뀐 이미지의 `orphaned_at`을 다시 계산한다
  (연결되면 NULL, 마지막 연결이 끊기면 그 시각 기록; images/repository.py `refresh_orphaned`, AC11).
- 새로 연결할 이미지는 확인할 때부터 행을 잠가, 정리 작업(`FOR UPDATE SKIP LOCKED`)이
  확인과 쓰기 사이에 지우지 못하게 한다 (images/repository.py `find_owned_images ... FOR SHARE`).
"""

import secrets

import psycopg
import pytest
from psycopg.rows import dict_row

from app.features.creation.user_profile import service
from app.features.creation.user_profile.schemas import AuthorizedStory, PresetDraft

OWNER_ID = 100


# ---------------------------------------------------------------- 준비 도우미


def _story(db, owner_id=OWNER_ID) -> AuthorizedStory:
    row = db.execute(
        "INSERT INTO stories(owner_id, title) VALUES (%s, %s) RETURNING id", (owner_id, "가상")
    ).fetchone()
    return AuthorizedStory(story_id=row["id"], owner_id=owner_id)


def _image(db, mime="image/png", owner_id=OWNER_ID, *, orphaned=True) -> int:
    """업로드만 된 이미지 행. 업로드 직후처럼 미사용 시각을 30시간 전으로 둔다."""
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}[mime]
    return db.execute(
        """INSERT INTO images(owner_id, storage_key, mime_type, byte_size, orphaned_at)
           VALUES (%s, %s, %s, 1, CASE WHEN %s THEN clock_timestamp() - interval '30 hours' END)
           RETURNING id""",
        (owner_id, f"{secrets.token_hex(16)}.{ext}", mime, orphaned),
    ).fetchone()["id"]


def _preset(db, story: AuthorizedStory, image_id, position=0, name="민수") -> int:
    return db.execute(
        """INSERT INTO story_user_presets(story_id, name, image_id, position)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (story.story_id, name, image_id, position),
    ).fetchone()["id"]


def _orphaned_at(db, image_id):
    return db.execute("SELECT orphaned_at FROM images WHERE id = %s", (image_id,)).fetchone()[
        "orphaned_at"
    ]


def _preset_rows(db, story: AuthorizedStory):
    return db.execute(
        "SELECT id, name, image_id, position FROM story_user_presets "
        "WHERE story_id = %s ORDER BY position",
        (story.story_id,),
    ).fetchall()


def _save(db_url, story, drafts, character_names=()):
    """호출 규약대로 호출자가 연 트랜잭션 안에서 교체하고 커밋한다."""
    with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.transaction():
            return service.replace_presets(
                conn, story, drafts, character_names=list(character_names)
            )


# ---------------------------------------------------------------- GIF 금지


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/webp"])
def test_owned_still_image_can_attach_to_preset_CREATE_US_05(db, db_url, mime):
    # 수정이 지나치게 막지 않는지 지키는 기준선.
    story = _story(db)
    image = _image(db, mime)
    result = _save(db_url, story, [PresetDraft(name="민수", image_id=image)])
    assert [p.image_id for p in result.presets] == [image]


def test_owned_gif_cannot_attach_to_new_preset_CREATE_US_05(db, db_url):
    story = _story(db)
    gif = _image(db, "image/gif")
    with pytest.raises(service.InvalidPresetImageError) as exc:
        _save(db_url, story, [PresetDraft(name="민수", image_id=gif)])
    assert exc.value.status == 422
    assert exc.value.extra.get("image_ids") == [gif]
    assert _preset_rows(db, story) == []
    assert _orphaned_at(db, gif) is not None  # 연결도 미사용 표시도 바뀌지 않는다


def test_owned_gif_cannot_replace_image_of_existing_preset_CREATE_US_05(db, db_url):
    story = _story(db)
    png = _image(db, orphaned=False)
    gif = _image(db, "image/gif")
    preset_id = _preset(db, story, png)
    before = _preset_rows(db, story)
    with pytest.raises(service.InvalidPresetImageError) as exc:
        _save(db_url, story, [PresetDraft(id=preset_id, name="민수", image_id=gif)])
    assert exc.value.extra.get("image_ids") == [gif]
    assert _preset_rows(db, story) == before


def test_only_gif_is_reported_when_mixed_with_still_images_CREATE_US_05(db, db_url):
    story = _story(db)
    png = _image(db)
    gif = _image(db, "image/gif")
    with pytest.raises(service.InvalidPresetImageError) as exc:
        _save(
            db_url,
            story,
            [PresetDraft(name="가", image_id=png), PresetDraft(name="나", image_id=gif)],
        )
    assert exc.value.extra.get("image_ids") == [gif]
    assert _preset_rows(db, story) == []


# ---------------------------------------------------------------- 미사용 표시(orphaned_at) 수명주기


def test_attaching_image_clears_orphan_mark_CREATE_US_05(db, db_url):
    story = _story(db)
    image = _image(db)
    assert _orphaned_at(db, image) is not None
    _save(db_url, story, [PresetDraft(name="민수", image_id=image)])
    assert _orphaned_at(db, image) is None


def test_clearing_all_presets_marks_detached_image_orphaned_CREATE_US_05(db, db_url):
    story = _story(db)
    image = _image(db, orphaned=False)
    _preset(db, story, image)
    _save(db_url, story, [])
    assert _orphaned_at(db, image) is not None


def test_removing_one_preset_marks_only_its_image_orphaned_CREATE_US_05(db, db_url):
    story = _story(db)
    kept_image = _image(db, orphaned=False)
    dropped_image = _image(db, orphaned=False)
    kept = _preset(db, story, kept_image, position=0, name="가")
    _preset(db, story, dropped_image, position=1, name="나")
    _save(db_url, story, [PresetDraft(id=kept, name="가", image_id=kept_image)])
    assert _orphaned_at(db, kept_image) is None
    assert _orphaned_at(db, dropped_image) is not None


def test_swapping_preset_image_marks_old_and_clears_new_CREATE_US_05(db, db_url):
    story = _story(db)
    old = _image(db, orphaned=False)
    new = _image(db)
    preset_id = _preset(db, story, old)
    _save(db_url, story, [PresetDraft(id=preset_id, name="민수", image_id=new)])
    assert _orphaned_at(db, old) is not None
    assert _orphaned_at(db, new) is None


def test_removing_image_from_preset_keeps_draft_and_marks_image_CREATE_US_05(db, db_url):
    # 이미지 없이 초안으로 남겨도 끊긴 이미지는 미사용이 된다.
    story = _story(db)
    image = _image(db, orphaned=False)
    preset_id = _preset(db, story, image)
    _save(db_url, story, [PresetDraft(id=preset_id, name="민수", image_id=None)])
    assert [r["image_id"] for r in _preset_rows(db, story)] == [None]
    assert _orphaned_at(db, image) is not None


def test_detached_image_still_used_elsewhere_is_not_marked_CREATE_US_05(db, db_url):
    story = _story(db)
    shared = _image(db, orphaned=False)
    _preset(db, story, shared)
    db.execute(
        "INSERT INTO story_images(story_id, image_id, position) VALUES (%s, %s, 0)",
        (story.story_id, shared),
    )
    _save(db_url, story, [])
    assert _orphaned_at(db, shared) is None


def test_rejected_save_leaves_orphan_marks_untouched_CREATE_US_05(db, db_url):
    story = _story(db)
    linked = _image(db, orphaned=False)
    _preset(db, story, linked)
    with pytest.raises(service.InvalidPresetReferenceError):
        _save(db_url, story, [PresetDraft(id=999_999, name="x")])
    assert _orphaned_at(db, linked) is None


# ---------------------------------------------------------------- 정리 작업과의 경합(행 잠금)


def test_attached_image_is_locked_before_first_write_CREATE_US_05(db, db_url, monkeypatch):
    """소유 확인 뒤 첫 쓰기 전에, 정리 작업이 쓰는 잠금(`FOR UPDATE SKIP LOCKED`)이 그 이미지를
    건너뛰어야 한다. 잠그지 않으면 정리가 확인과 INSERT 사이에 행을 지워 FK 오류(500)가 난다.
    (INSERT 후에는 FK가 KEY SHARE 잠금을 잡으므로, 반드시 쓰기 전 시점에서 확인한다.)
    """
    story = _story(db)
    image = _image(db)
    probes: list[object] = []
    original = service.repo.delete_presets_except

    def probe_then_delete(conn, story_id, keep_ids):
        with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as cleaner:
            with cleaner.transaction():
                probes.append(
                    cleaner.execute(
                        "SELECT id FROM images WHERE id = %s FOR UPDATE SKIP LOCKED", (image,)
                    ).fetchone()
                )
        return original(conn, story_id, keep_ids)

    monkeypatch.setattr(service.repo, "delete_presets_except", probe_then_delete)
    _save(db_url, story, [PresetDraft(name="민수", image_id=image)])
    assert probes == [None], "정리 작업이 연결 중인 이미지 행을 잠글 수 있었다"
