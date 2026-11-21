"""CREATE-US-06 AC01·03·05~10 스토리·캐릭터 이미지 목록 저장의 실제 PostgreSQL 통합검사."""

import pytest

from tests.features.creation.images.helpers import (
    GIF,
    OTHER_ID,
    PNG,
    create_story,
    uploaded_id,
)


def url(story_id):
    return f"/api/v1/creation/stories/{story_id}/images"


def put(api, story_id, story_ids=(), characters=None, key="request-one"):
    body = {
        "story_image_ids": list(story_ids),
        "characters": [
            {"character_id": cid, "image_ids": list(ids)} for cid, ids in (characters or {}).items()
        ],
    }
    return api.put(url(story_id), json=body, headers={"Idempotency-Key": key})


def ids_of(items):
    return [item["image_id"] for item in items]


def orphaned(db, image_id):
    return db.execute("SELECT orphaned_at FROM images WHERE id=%s", (image_id,)).fetchone()[
        "orphaned_at"
    ]


def test_save_and_reload_keeps_targets_order_and_first_as_cover_CREATE_US_06_AC01_AC06(api, db):
    story_id, (harin, doyun) = create_story(db)
    s1, s2, s3 = (uploaded_id(api, target="story") for _ in range(3))
    h1, h2 = uploaded_id(api), uploaded_id(api)

    saved = put(api, story_id, [s2, s3, s1], {harin: [h2, h1]})
    assert saved.status_code == 200, saved.text

    # 화면을 닫았다가 다시 여는 것과 같은 새 조회.
    body = api.get(url(story_id)).json()
    assert ids_of(body["story_images"]) == [s2, s3, s1]
    characters = {c["character_id"]: c for c in body["characters"]}
    assert ids_of(characters[harin]["images"]) == [h2, h1]
    assert characters[doyun]["images"] == []
    assert body["cover"] == {
        "source": "story",
        "character_id": None,
        "image": body["story_images"][0],
    }
    # 연결된 이미지는 미사용 상태가 아니다.
    assert all(orphaned(db, i) is None for i in (s1, s2, s3, h1, h2))


def test_story_gif_can_move_anywhere_in_order_CREATE_US_06_AC05_AC06(api, db):
    story_id, _ = create_story(db)
    still = uploaded_id(api, target="story")
    gif = uploaded_id(api, GIF, target="story")
    assert ids_of(put(api, story_id, [still, gif]).json()["story_images"]) == [still, gif]
    moved = put(api, story_id, [gif, still], key="request-two").json()
    assert ids_of(moved["story_images"]) == [gif, still]
    assert moved["cover"]["image"]["mime_type"] == "image/gif"


def test_gif_cannot_be_linked_to_character_CREATE_US_06_AC05(api, db):
    story_id, (harin, _) = create_story(db)
    gif = uploaded_id(api, GIF, target="story")
    response = put(api, story_id, [], {harin: [gif]})
    assert response.status_code == 422
    assert response.json()["code"] == "IMAGE_GIF_NOT_ALLOWED"
    assert response.json()["issues"] == [{"character_id": harin, "image_id": gif}]


def test_count_limits_story_ten_character_five_CREATE_US_06_AC03(api, db):
    story_id, (harin, _) = create_story(db)
    story_images = [uploaded_id(api, target="story") for _ in range(11)]
    face_images = [uploaded_id(api) for _ in range(6)]

    assert put(api, story_id, story_images[:10]).status_code == 200
    assert put(api, story_id, story_images, key="r2").status_code == 422
    assert put(api, story_id, [], {harin: face_images[:5]}, key="r3").status_code == 200
    assert put(api, story_id, [], {harin: face_images}, key="r4").status_code == 422
    assert put(api, story_id, [story_images[0], story_images[0]], key="r5").status_code == 422


def test_replace_keeps_slot_and_order_and_deletes_old_on_save_CREATE_US_06_AC07(api, db, storage):
    story_id, (harin, _) = create_story(db)
    a, b, c = (uploaded_id(api) for _ in range(3))
    put(api, story_id, [], {harin: [a, b, c]})
    old_key = db.execute("SELECT storage_key FROM images WHERE id=%s", (b,)).fetchone()

    new = uploaded_id(api)
    replaced = put(api, story_id, [], {harin: [a, new, c]}, key="replace")
    assert replaced.status_code == 200
    harin_images = replaced.json()["characters"][0]["images"]
    assert ids_of(harin_images) == [a, new, c]
    # 2026-11-20 결정: 예전 사진은 보관하지 않고 저장할 때 바로 지운다(파일과 행 모두).
    assert db.execute("SELECT 1 FROM images WHERE id=%s", (b,)).fetchone() is None
    assert storage.path_of(old_key["storage_key"]) is None
    assert orphaned(db, new) is None


def test_failed_upload_does_not_touch_existing_list_CREATE_US_06_AC07(api, db):
    story_id, (harin, _) = create_story(db)
    a = uploaded_id(api)
    put(api, story_id, [], {harin: [a]})
    # 교체용 업로드가 실패하면 목록 저장 요청 자체가 없으므로 기존 이미지가 그대로다.
    failed = api.post(
        "/api/v1/creation/images",
        params={"target": "character"},
        files={"file": ("big.png", PNG + b"\x00" * (5 * 1024 * 1024), "image/png")},
    )
    assert failed.status_code == 422
    assert ids_of(api.get(url(story_id)).json()["characters"][0]["images"]) == [a]


def test_draft_allows_deleting_last_image_and_reports_missing_CREATE_US_06_AC08(api, db):
    story_id, (harin, doyun) = create_story(db)
    a, b = uploaded_id(api), uploaded_id(api)
    put(api, story_id, [], {harin: [a], doyun: [b]})

    emptied = put(api, story_id, [], {harin: []}, key="delete-last")
    assert emptied.status_code == 200
    body = emptied.json()
    assert body["characters_missing_images"] == [harin]
    # 보내지 않은 캐릭터의 이미지는 바뀌지 않는다.
    assert ids_of(body["characters"][1]["images"]) == [b]
    # 미등록 스토리는 저장이 곧 반영이므로 지운 사진은 바로 정리된다.
    assert db.execute("SELECT 1 FROM images WHERE id=%s", (a,)).fetchone() is None


def test_cover_falls_back_to_first_character_first_image_CREATE_US_06_AC09(api, db):
    story_id, (harin, doyun) = create_story(db)
    h1, h2, d1 = uploaded_id(api), uploaded_id(api), uploaded_id(api)
    body = put(api, story_id, [], {harin: [h1, h2], doyun: [d1]}).json()
    assert body["cover"]["source"] == "character"
    assert body["cover"]["character_id"] == harin
    assert body["cover"]["image"]["image_id"] == h1
    # 대체 표지를 스토리 이미지로 복제 연결하지 않는다.
    assert db.execute("SELECT count(*) AS n FROM story_images").fetchone()["n"] == 0

    no_images = create_story(db, characters=("민준",))[0]
    assert api.get(url(no_images)).json()["cover"] is None


def test_image_shared_by_other_target_stays_in_use_CREATE_US_06_AC10(api, db):
    story_id, (harin, _) = create_story(db)
    shared = uploaded_id(api)
    put(api, story_id, [shared], {harin: [shared]})

    put(api, story_id, [], key="remove-from-story")
    # 캐릭터 연결이 남아 있으므로 사용 중이고, 캐릭터 쪽 연결도 그대로다.
    assert orphaned(db, shared) is None
    assert ids_of(api.get(url(story_id)).json()["characters"][0]["images"]) == [shared]


def test_other_users_images_and_stories_are_hidden(api, db):
    story_id, (harin, _) = create_story(db)
    others = uploaded_id(api, headers={"X-User-Id": str(OTHER_ID)})
    response = put(api, story_id, [], {harin: [others]})
    assert response.status_code == 422
    assert response.json()["code"] == "IMAGE_NOT_FOUND"
    assert response.json()["image_ids"] == [others]

    other_story, _ = create_story(db, owner_id=OTHER_ID)
    assert api.get(url(other_story)).status_code == 404
    assert put(api, other_story, [], key="other").status_code == 404


def test_character_must_belong_to_story(api, db):
    story_id, _ = create_story(db)
    _, (stranger, _) = create_story(db)
    response = put(api, story_id, [], {stranger: []})
    assert response.status_code == 422
    assert response.json()["code"] == "CHARACTER_NOT_FOUND"


def test_replay_returns_same_result_and_reused_key_is_rejected(api, db):
    story_id, _ = create_story(db)
    a, b = uploaded_id(api, target="story"), uploaded_id(api, target="story")
    first = put(api, story_id, [a], key="same")
    assert put(api, story_id, [a], key="same").json() == first.json()
    reused = put(api, story_id, [b], key="same")
    assert reused.status_code == 409
    assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def complete_story(db, story_id):
    """기본 정보 완성 조건(제목·소개·세계관·캐릭터 상세)을 채운다. 이미지 조건만 남긴다."""
    db.execute(
        "UPDATE stories SET subtitle='소개', world_description='세계관' WHERE id=%s", (story_id,)
    )
    db.execute("UPDATE characters SET description='상세' WHERE story_id=%s", (story_id,))


def common_save(api, story_id, state, key):
    body = {
        "expression": {
            "point_of_view": "third_person",
            "tense": "past",
            "composition": "balanced",
            "response_length": "normal",
            "style_mode": "none",
            "preset_styles": [],
            "custom_style": None,
        },
        "fewshots": [],
        "intros": [
            {
                "id": None,
                "client_key": "intro",
                "title": "첫 만남",
                "messages": [
                    {
                        "id": None,
                        "client_key": "m",
                        "speaker_kind": "narrator",
                        "character_id": None,
                        "body": "비가 내린다.",
                    }
                ],
                "scene": {},
            }
        ],
        "relations": [],
        "affinities": [],
        "save_state": state,
        "mode": "manual",
    }
    return api.put(
        f"/api/v1/creation/stories/{story_id}/creation",
        json=body,
        headers={"Idempotency-Key": key},
    )


@pytest.mark.parametrize("state", ["private", "public"])
def test_completed_save_requires_image_per_character_CREATE_US_06_AC08(api, db, state):
    story_id, (harin, doyun) = create_story(db)
    complete_story(db, story_id)
    put(api, story_id, [], {harin: [uploaded_id(api)]})

    rejected = common_save(api, story_id, state, "first")
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "CHARACTER_IMAGE_REQUIRED"
    image_issues = [i for i in rejected.json()["issues"] if i["code"] == "CHARACTER_IMAGE_REQUIRED"]
    # 누락 대상(두 번째 캐릭터 도윤)을 알려 주고, 임시 저장 상태와 작성 내용은 그대로다.
    assert [(i["field"], i["value"]) for i in image_issues] == [
        ("characters[1].images", str(doyun))
    ]
    assert (
        db.execute("SELECT save_state FROM stories WHERE id=%s", (story_id,)).fetchone()[
            "save_state"
        ]
        == "draft"
    )
    assert ids_of(api.get(url(story_id)).json()["characters"][0]["images"]) != []

    put(api, story_id, [], {doyun: [uploaded_id(api)]}, key="fill")
    accepted = common_save(api, story_id, state, "second")
    assert accepted.status_code == 200, accepted.text


def test_completed_story_cannot_lose_last_character_image_CREATE_US_06_AC08(api, db):
    story_id, (harin, doyun) = create_story(db)
    complete_story(db, story_id)
    h1, h2, d1 = uploaded_id(api), uploaded_id(api), uploaded_id(api)
    put(api, story_id, [], {harin: [h1, h2], doyun: [d1]})
    assert common_save(api, story_id, "private", "publish").status_code == 200

    # 완성 상태에서도 필수 조건을 지키는 변경(순서 변경·한 장 삭제)은 저장된다.
    reordered = put(api, story_id, [], {harin: [h2]}, key="keep-one")
    assert reordered.status_code == 200
    assert ids_of(reordered.json()["characters"][0]["images"]) == [h2]

    # 마지막 이미지를 지우는 저장은 거절하고 기존 목록을 그대로 둔다.
    emptied = put(api, story_id, [], {doyun: []}, key="remove-last")
    assert emptied.status_code == 422
    assert emptied.json()["code"] == "CHARACTER_IMAGE_REQUIRED"
    assert emptied.json()["issues"][0]["field"] == "characters[1].images"
    assert ids_of(api.get(url(story_id)).json()["characters"][1]["images"]) == [d1]
    assert orphaned(db, d1) is None
