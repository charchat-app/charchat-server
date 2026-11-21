"""CREATE-US-06 이미지 파일 조회 권한과 캐시의 실제 PostgreSQL 통합검사.

공개(public)이면서 삭제되지 않은 스토리의 반영본에 연결된 이미지만 누구나 볼 수 있다.
미등록·비공개 스토리, 아직 연결하지 않은 업로드, 개인 프로필 사진은 소유자만 볼 수 있다.
"""

import pytest

from tests.features.creation.images.helpers import OTHER_ID, OWNER_ID, PNG, create_story, upload

PUBLIC_CACHE = "public, max-age=31536000, immutable"
PRIVATE_CACHE = "private, no-store"


def uploaded(api, target="story"):
    response = upload(api, PNG, target)
    assert response.status_code == 201, response.text
    body = response.json()
    return body["image_id"], body["url"]


def fetch(api, url, viewer):
    """viewer: 'owner' | 'other' | 'anonymous'."""
    user_ids = {"owner": OWNER_ID, "other": OTHER_ID}
    headers = {"X-User-Id": str(user_ids[viewer])} if viewer in user_ids else {}
    # 픽스처의 기본 헤더(소유자)가 섞이지 않도록 잠시 비우고 보낸다.
    saved = api.headers
    api.headers = {}
    try:
        return api.get(url, headers=headers)
    finally:
        api.headers = saved


def link_story_image(db, story_id, image_id):
    db.execute(
        "INSERT INTO story_images(story_id, image_id, position) VALUES (%s, %s, 0)",
        (story_id, image_id),
    )


def link_character_image(db, character_id, image_id):
    db.execute(
        "INSERT INTO character_images(character_id, image_id, position) VALUES (%s, %s, 0)",
        (character_id, image_id),
    )


def assert_visible_to_all_with_public_cache(api, url):
    for viewer in ("owner", "other", "anonymous"):
        response = fetch(api, url, viewer)
        assert response.status_code == 200, (viewer, response.text)
        assert response.content == PNG
        assert response.headers["cache-control"] == PUBLIC_CACHE


def assert_owner_only_without_public_cache(api, url):
    owner = fetch(api, url, "owner")
    assert owner.status_code == 200, owner.text
    assert owner.content == PNG
    assert owner.headers["cache-control"] == PRIVATE_CACHE
    assert "public" not in owner.headers["cache-control"]
    for viewer in ("other", "anonymous"):
        response = fetch(api, url, viewer)
        # 이미지가 있는지도 알리지 않도록 없는 이미지와 같은 404로 응답한다.
        assert response.status_code == 404, (viewer, response.text)
        assert response.json()["code"] == "IMAGE_NOT_FOUND"
        assert "public" not in response.headers.get("cache-control", "")


def test_unlinked_upload_is_owner_only_CREATE_US_06(api):
    _, url = uploaded(api)
    assert_owner_only_without_public_cache(api, url)


@pytest.mark.parametrize("save_state", ["draft", "private"])
def test_story_and_character_images_of_unpublished_story_are_owner_only_CREATE_US_06(
    api, db, save_state
):
    story_id, (harin, _) = create_story(db, save_state=save_state)
    story_image, story_url = uploaded(api, "story")
    face_image, face_url = uploaded(api, "character")
    link_story_image(db, story_id, story_image)
    link_character_image(db, harin, face_image)

    assert_owner_only_without_public_cache(api, story_url)
    assert_owner_only_without_public_cache(api, face_url)


def test_story_and_character_images_of_public_story_are_visible_to_all_CREATE_US_06(api, db):
    story_id, (harin, _) = create_story(db, save_state="public")
    story_image, story_url = uploaded(api, "story")
    face_image, face_url = uploaded(api, "character")
    link_story_image(db, story_id, story_image)
    link_character_image(db, harin, face_image)

    assert_visible_to_all_with_public_cache(api, story_url)
    assert_visible_to_all_with_public_cache(api, face_url)


def test_preset_image_of_public_story_is_visible_to_all_CREATE_US_06(api, db):
    story_id, _ = create_story(db, save_state="public")
    image_id, url = uploaded(api, "profile")
    db.execute(
        """INSERT INTO story_user_presets(story_id, name, image_id, position)
           VALUES (%s, '프리셋', %s, 0)""",
        (story_id, image_id),
    )
    assert_visible_to_all_with_public_cache(api, url)


def test_personal_profile_image_is_owner_only_CREATE_US_06(api, db):
    image_id, url = uploaded(api, "profile")
    db.execute(
        "INSERT INTO user_profiles(owner_id, name, image_id) VALUES (%s, '나', %s)",
        (OWNER_ID, image_id),
    )
    assert_owner_only_without_public_cache(api, url)


def test_image_of_deleted_public_story_is_owner_only_CREATE_US_06(api, db):
    story_id, _ = create_story(db, save_state="public")
    image_id, url = uploaded(api, "story")
    link_story_image(db, story_id, image_id)

    db.execute("UPDATE stories SET deleted_at=clock_timestamp() WHERE id=%s", (story_id,))
    assert_owner_only_without_public_cache(api, url)


def test_image_shared_by_public_and_private_story_stays_public_CREATE_US_06(api, db):
    public_id, _ = create_story(db, save_state="public")
    private_id, _ = create_story(db, save_state="private")
    image_id, url = uploaded(api, "story")
    link_story_image(db, public_id, image_id)
    link_story_image(db, private_id, image_id)
    assert_visible_to_all_with_public_cache(api, url)


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_malformed_user_header_is_rejected_on_file_fetch(api, value):
    _, url = uploaded(api)
    saved = api.headers
    api.headers = {}
    try:
        response = api.get(url, headers={"X-User-Id": value})
    finally:
        api.headers = saved
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


def test_image_only_in_working_copy_of_public_story_is_owner_only_CREATE_US_06(api, db):
    """공개 스토리를 고치다 임시 저장한 사진은 저장 전까지 소유자만 본다.

    이미 반영된 사진은 계속 누구나 볼 수 있다.
    """
    story_id, (harin, doyun) = create_story(db)
    published, published_url = uploaded(api, "character")
    other_face, _ = uploaded(api, "character")
    body = {
        "story_image_ids": [],
        "characters": [
            {"character_id": harin, "image_ids": [published]},
            {"character_id": doyun, "image_ids": [other_face]},
        ],
        "kind": "save",
    }
    url = f"/api/v1/creation/stories/{story_id}/images"
    assert api.put(url, json=body, headers={"Idempotency-Key": "seed"}).status_code == 200
    db.execute("UPDATE stories SET save_state='public' WHERE id=%s", (story_id,))

    draft_only, draft_url = uploaded(api, "character")
    body["characters"][0]["image_ids"] = [draft_only]
    body["kind"] = "temporary"
    assert api.put(url, json=body, headers={"Idempotency-Key": "temp"}).status_code == 200

    assert_owner_only_without_public_cache(api, draft_url)
    assert_visible_to_all_with_public_cache(api, published_url)
