"""안 B(2026-11-20 이휘 제안, 팀 확인 전): 공통 「저장」이 사진 임시 저장본까지 함께 검사·반영한다.

- 임시 저장 중에는 이용자에게 이전 사진이 보인다.
- 「저장」에 성공한 뒤에만 새 사진이 보이고, 연결이 끊긴 예전 사진은 바로 지운다.
- 다른 곳에서 계속 쓰는 예전 파일은 지우지 않는다.
- 검사·저장이 실패하면 2~4번 내용과 사진 모두 반영하지 않고 임시 저장본을 남긴다.
"""

import pytest

from tests.features.creation.images.helpers import OWNER_ID, PNG, upload
from tests.features.creation.images.test_image_file_access import fetch
from tests.features.creation.speech_style.test_common_save_api import body


def images_url(story_id):
    return f"/api/v1/creation/stories/{story_id}/images"


def creation_url(story_id):
    return f"/api/v1/creation/stories/{story_id}/creation"


def uploaded(api):
    response = upload(api, PNG, "character")
    assert response.status_code == 201, response.text
    return response.json()["image_id"], response.json()["url"]


def put_images(api, story_id, characters, kind, key):
    payload = {
        "story_image_ids": [],
        "characters": [{"character_id": c, "image_ids": ids} for c, ids in characters.items()],
        "kind": kind,
    }
    return api.put(images_url(story_id), json=payload, headers={"Idempotency-Key": key})


def save_creation(api, story_id, state, text, key, kind="save"):
    payload = {**body(state, text=text), "kind": kind}
    return api.put(creation_url(story_id), json=payload, headers={"Idempotency-Key": key})


def linked(db, character_id):
    rows = db.execute(
        "SELECT image_id FROM character_images WHERE character_id=%s ORDER BY position",
        (character_id,),
    ).fetchall()
    return [row["image_id"] for row in rows]


def exists(db, image_id):
    return db.execute("SELECT 1 FROM images WHERE id=%s", (image_id,)).fetchone() is not None


def custom_style(db, story_id):
    row = db.execute(
        "SELECT custom_style FROM story_expression_settings WHERE story_id=%s", (story_id,)
    ).fetchone()
    return row["custom_style"] if row else None


def add_character(db, story_id, name, position):
    return db.execute(
        """INSERT INTO characters(story_id, name, description, position)
           VALUES (%s, %s, '상세', %s) RETURNING id""",
        (story_id, name, position),
    ).fetchone()["id"]


@pytest.fixture
def story(api, db):
    """하린 a, 도윤 b가 연결되고 문체가 「반영된 문체」인 완성(state) 스토리를 만든다."""

    def make(state="private"):
        story_id = db.execute(
            """INSERT INTO stories(owner_id, title, subtitle, world_description)
               VALUES (%s, '가상 스토리', '한 줄 소개', '세계관') RETURNING id""",
            (OWNER_ID,),
        ).fetchone()["id"]
        harin = add_character(db, story_id, "하린", 0)
        doyun = add_character(db, story_id, "도윤", 1)
        (a, a_url), (b, _) = uploaded(api), uploaded(api)
        assert (
            put_images(api, story_id, {harin: [a], doyun: [b]}, "save", "seed").status_code == 200
        )
        published = save_creation(api, story_id, state, "반영된 문체", "publish")
        assert published.status_code == 200, published.text
        return {"id": story_id, "harin": harin, "doyun": doyun, "a": a, "a_url": a_url, "b": b}

    return make


def test_save_applies_temporary_photos_together_with_creation(api, db, storage, story):
    s = story()
    c, _ = uploaded(api)
    assert put_images(api, s["id"], {s["harin"]: [c]}, "temporary", "temp").status_code == 200
    # 임시 저장 중: 이용자 쪽 연결은 그대로이고, 편집 화면은 「저장」할 사진이 있다는 것을 안다.
    assert linked(db, s["harin"]) == [s["a"]]
    opened = api.get(creation_url(s["id"])).json()
    assert opened["images_working_copy_saved_at"] is not None

    saved = save_creation(api, s["id"], "private", "새 문체", "commit")
    assert saved.status_code == 200, saved.text
    assert saved.json()["images_working_copy_saved_at"] is None
    assert linked(db, s["harin"]) == [c]
    assert linked(db, s["doyun"]) == [s["b"]]
    assert custom_style(db, s["id"]) == "새 문체"
    assert db.execute("SELECT count(*) AS n FROM story_working_copies").fetchone()["n"] == 0
    # 연결이 끊긴 예전 사진은 행과 파일 모두 바로 지운다(2026-11-20 결정: 보관하지 않음).
    assert not exists(db, s["a"])
    assert not any(storage.root.glob(f"*{s['a_url'].rsplit('/', 1)[-1]}"))


def test_completion_check_uses_temporary_photos(api, db, story):
    """안 A에서 생기던 문제: 편집 화면에는 사진이 있는데 반영본 기준 검사로 거절되던 경우."""
    s = story()
    # 기본 정보 저장으로 새 캐릭터가 바로 반영됐고, 그 캐릭터의 사진은 임시 저장만 된 상태
    seah = add_character(db, s["id"], "세아", 2)
    d, _ = uploaded(api)
    assert put_images(api, s["id"], {seah: [d]}, "temporary", "temp").status_code == 200

    saved = save_creation(api, s["id"], "private", "반영된 문체", "commit")
    assert saved.status_code == 200, saved.text
    assert linked(db, seah) == [d]


def test_failed_check_applies_nothing_and_keeps_working_copy(api, db, story):
    s = story()
    # 도윤의 마지막 사진을 지운 목록을 임시 저장한 뒤 「저장」
    assert put_images(api, s["id"], {s["doyun"]: []}, "temporary", "temp").status_code == 200

    rejected = save_creation(api, s["id"], "private", "반영되면 안 되는 문체", "commit")
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "CHARACTER_IMAGE_REQUIRED"
    # 2~4번 내용도, 사진도 반영하지 않는다.
    assert custom_style(db, s["id"]) == "반영된 문체"
    assert linked(db, s["doyun"]) == [s["b"]]
    assert exists(db, s["b"])
    # 임시 저장본은 남아 계속 고칠 수 있다.
    reopened = api.get(images_url(s["id"])).json()
    assert reopened["working_copy_saved_at"] is not None
    assert api.get(creation_url(s["id"])).json()["images_working_copy_saved_at"] is not None


def test_public_story_shows_old_photo_until_saved_then_only_new_photo(api, db, story):
    s = story("public")
    c, c_url = uploaded(api)
    assert put_images(api, s["id"], {s["harin"]: [c]}, "temporary", "temp").status_code == 200

    # 임시 저장 중: 이용자(비로그인)에게는 이전 사진만 보인다.
    assert fetch(api, s["a_url"], "anonymous").status_code == 200
    assert fetch(api, c_url, "anonymous").status_code == 404

    assert save_creation(api, s["id"], "public", "반영된 문체", "commit").status_code == 200
    # 반영 뒤: 새 사진만 보이고 예전 사진은 지워졌다.
    assert fetch(api, c_url, "anonymous").status_code == 200
    assert fetch(api, s["a_url"], "anonymous").status_code == 404
    assert fetch(api, s["a_url"], "owner").status_code == 404


def test_old_photo_still_used_elsewhere_is_not_deleted(api, db, storage, story):
    s = story("public")
    # 예전 사진 a를 개인 프로필에서도 쓰고 있다.
    db.execute(
        "INSERT INTO user_profiles(owner_id, name, image_id) VALUES (%s, '나', %s)",
        (OWNER_ID, s["a"]),
    )
    c, _ = uploaded(api)
    assert put_images(api, s["id"], {s["harin"]: [c]}, "temporary", "temp").status_code == 200
    assert save_creation(api, s["id"], "public", "반영된 문체", "commit").status_code == 200

    assert linked(db, s["harin"]) == [c]
    assert exists(db, s["a"])
    owner = fetch(api, s["a_url"], "owner")
    assert owner.status_code == 200 and owner.content == PNG
    # 더 이상 공개 스토리에 연결되지 않았으므로 소유자만 본다.
    assert fetch(api, s["a_url"], "anonymous").status_code == 404


def test_temporary_creation_save_keeps_photo_working_copy(api, db, story):
    s = story()
    c, _ = uploaded(api)
    assert put_images(api, s["id"], {s["harin"]: [c]}, "temporary", "temp").status_code == 200
    temporary = save_creation(api, s["id"], "private", "고치는 중", "temp-2", kind="temporary")
    assert temporary.status_code == 200
    assert temporary.json()["images_working_copy_saved_at"] is not None
    # 임시 저장은 사진도 반영하지 않는다.
    assert linked(db, s["harin"]) == [s["a"]]


def test_replayed_save_request_does_not_apply_twice(api, db, story):
    s = story()
    c, _ = uploaded(api)
    assert put_images(api, s["id"], {s["harin"]: [c]}, "temporary", "temp").status_code == 200
    first = save_creation(api, s["id"], "private", "새 문체", "commit")
    again = save_creation(api, s["id"], "private", "새 문체", "commit")
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert linked(db, s["harin"]) == [c]
