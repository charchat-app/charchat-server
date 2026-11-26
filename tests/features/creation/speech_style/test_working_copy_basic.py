"""CREATE-US-07 2~4번 공통 저장에 1번 기본 정보(`basic`)를 함께 싣는 임시 저장·저장 통합검사.

대상 인수 조건: CREATE-US-07-AC04, AC07, AC08, AC09 (서버 첫 조각).
- `basic`은 선택 항목이며 모양은 기본 정보 수정 요청(StoryUpdateRequest)과 같다.
  (title, subtitle, world_description, tags, characters)
- 비공개·공개 스토리의 임시 저장은 잘못된 기본 정보도 원문 그대로 임시 저장본에 둔다.
  이용자가 보는 저장본(GET /stories/{id})은 바뀌지 않는다.
- `basic` 없이 보낸 다른 임시 저장·자동 저장은 이미 임시 저장한 기본 정보를 지우지 않는다.
- 저장에서 `basic`을 빼면 가장 최근에 임시 저장한 기본 정보를 반영하고, 보내면 보낸 값이 우선한다.
- 저장이 거절되면 아무것도 반영하지 않고 임시 저장한 기본 정보를 그대로 남긴다.
"""

import psycopg
import pytest
from pydantic import ValidationError

from app.features.creation.save_visibility.schemas import CreationSaveRequest
from app.features.creation.save_visibility.service import request_digest
from tests.features.creation.speech_style.test_common_save_api import (
    body,
    complete,
    current_payload,
    put,
    url,
)
from tests.features.creation.speech_style.test_working_copy_presets import image, preset
from tests.features.creation.speech_style.test_working_copy_save import (
    publish,
    working_copy_rows,
)

ORIGINAL_BASIC = {
    "title": "가상 스토리",
    "subtitle": "한 줄 소개",
    "world_description": "수정하면 안 되는 세계관",
    "tags": [],
}


def basic_url(story):
    return f"/api/v1/creation/stories/{story['id']}"


def committed_basic(client, story):
    """이용자가 보는 저장본의 기본 정보(1번 GET)."""
    result = client.get(basic_url(story))
    assert result.status_code == 200, result.text
    data = result.json()
    return {key: data[key] for key in ORIGINAL_BASIC}


def staged(response_json):
    """2~4번 응답의 `basic`에서 비교할 필드만 꺼낸다."""
    basic = response_json["basic"]
    assert basic is not None
    return {key: basic[key] for key in ORIGINAL_BASIC}


def character_id(dsn, story):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT id FROM characters WHERE story_id=%s ORDER BY position", (story["id"],)
        ).fetchone()[0]


def stage(client, story, saved, basic, key):
    """완성 스토리에 기본 정보를 담아 임시 저장한다."""
    payload = {**current_payload(saved), "kind": "temporary", "basic": basic}
    result = put(client, story, payload, key)
    assert result.status_code == 200, result.text
    return result.json()


VALID_BASIC = {
    "title": "임시 제목",
    "subtitle": "임시 소개",
    "world_description": "임시 세계관",
    "tags": ["판타지"],
    "characters": None,
}


def test_draft_basic_temporary_cannot_drop_scene_roster_CREATE_US_07_AC08(context):
    client, _, story, _, dsn = context
    complete(dsn, story)
    person = character_id(dsn, story)
    result = put(
        client,
        story,
        {
            **body(),
            "kind": "temporary",
            "preserve_scene": True,
            "basic": {**VALID_BASIC, "characters": []},
        },
        "draft-basic-removal",
    )
    assert result.status_code == 422, result.text
    assert result.json()["code"] == "BASIC_CHARACTER_MEMBERSHIP_UNSUPPORTED"
    assert character_id(dsn, story) == person
    assert committed_basic(client, story) == ORIGINAL_BASIC


def test_draft_preserve_scene_autosave_allocates_new_character_once(context):
    client, _, story, _, dsn = context
    complete(dsn, story)
    original = character_id(dsn, story)
    before = client.get(url(story)).json()
    payload = {
        **body(),
        "kind": "temporary",
        "mode": "auto",
        "preserve_scene": True,
        "basic": {
            **VALID_BASIC,
            "characters": [
                {"id": original, "name": "첫 인물"},
                {"id": None, "name": "두 번째 인물"},
            ],
        },
    }
    saved = put(client, story, payload, "draft-roster-add")
    assert saved.status_code == 200, saved.text
    assert put(client, story, payload, "draft-roster-add").json() == saved.json()
    fresh = client.get(url(story)).json()
    assert fresh["save_state"] == "draft"
    new_id = saved.json()["characters"][1]["id"]
    assert [row["id"] for row in fresh["characters"]] == [original, new_id]
    assert [row["name"] for row in fresh["characters"]] == ["첫 인물", "두 번째 인물"]
    for section in ("expression", "intros", "fewshots", "relations", "affinities"):
        assert fresh[section] == before[section]
    # An older editor does not know the newly assigned ID. A different request must not remove it.
    stale = put(client, story, payload, "draft-roster-stale")
    assert stale.status_code == 422, stale.text
    assert client.get(url(story)).json()["characters"] == fresh["characters"]


def test_final_save_commits_basic_scene_presets_and_images_together_CREATE_US_07_AC07(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    payload = current_payload(saved)
    payload["expression"]["custom_style"] = "함께 반영된 문체"
    person = character_id(dsn, story)
    face, cover, profile = (image(dsn) for _ in range(3))
    result = put(
        client,
        story,
        {
            **payload,
            "basic": VALID_BASIC,
            "presets": [preset("{{user}}", profile)],
            "images": {
                "story_image_ids": [cover],
                "characters": [{"character_id": person, "image_ids": [face]}],
            },
        },
        "save-basic-scene-cover",
    )
    assert result.status_code == 200, result.text
    assert committed_basic(client, story)["title"] == VALID_BASIC["title"]
    reopened = client.get(url(story)).json()
    assert reopened["expression"]["custom_style"] == "함께 반영된 문체"
    assert reopened["presets"][0]["image_id"] == profile
    photos = client.get(f"{basic_url(story)}/images").json()
    assert photos["story_images"][0]["image_id"] == cover
    assert photos["characters"][0]["images"][0]["image_id"] == face


def test_auto_saves_basic_and_cover_without_promoting_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    cover = image(dsn)
    result = put(
        client,
        story,
        {
            **current_payload(saved),
            "kind": "temporary",
            "mode": "auto",
            "preserve_scene": True,
            "basic": VALID_BASIC,
            "images": {"story_image_ids": [cover]},
        },
        "auto-mixed-basic-cover",
    )
    assert result.status_code == 200, result.text
    assert result.json()["images_working_copy_saved_at"] is not None
    assert staged(client.get(url(story)).json())["title"] == VALID_BASIC["title"]
    assert committed_basic(client, story) == ORIGINAL_BASIC
    photos = client.get(f"{basic_url(story)}/images").json()
    assert photos["story_images"][0]["image_id"] == cover
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM story_images WHERE story_id = %s", (story["id"],)
            ).fetchone()
            is None
        )


def test_combined_final_rejection_preserves_pending_and_committed_CREATE_US_07_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    first = stage(client, story, saved, VALID_BASIC, "before-failed-combined")
    face, cover, profile = (image(dsn) for _ in range(3))
    person = character_id(dsn, story)
    photos_url = f"{basic_url(story)}/images"
    before = client.get(photos_url).json()
    rejected = put(
        client,
        story,
        {
            **current_payload(saved),
            "intros": [],
            "basic": VALID_BASIC,
            "presets": [preset("{{user}}", profile)],
            "images": {
                "story_image_ids": [cover],
                "characters": [{"character_id": person, "image_ids": [face]}],
            },
        },
        "failed-combined-final",
    )
    assert rejected.status_code == 422, rejected.text
    assert committed_basic(client, story) == ORIGINAL_BASIC
    assert client.get(photos_url).json() == before
    reopened = client.get(url(story)).json()
    assert reopened["basic"] == first["basic"]
    assert reopened["working_copy_saved_at"] == first["working_copy_saved_at"]
    assert reopened["presets"] == first["presets"]


def test_later_save_wins_without_replay_overwrite_CREATE_US_07_AC02(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    original_snapshot = current_payload(saved)
    first = {**original_snapshot, "basic": {**VALID_BASIC, "title": "먼저 저장"}}
    last = {**original_snapshot, "basic": {**VALID_BASIC, "title": "나중에 저장"}}
    assert put(client, story, first, "editor-one").status_code == 200
    assert put(client, story, last, "editor-two").status_code == 200
    assert put(client, story, first, "editor-one").status_code == 200
    assert committed_basic(client, story)["title"] == "나중에 저장"


def test_out_of_range_cover_and_face_ids_return_validation_error(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    outside = 9223372036854775808
    for images in (
        {"story_image_ids": [outside]},
        {"characters": [{"character_id": character_id(dsn, story), "image_ids": [outside]}]},
    ):
        response = put(client, story, {**current_payload(saved), "images": images}, "bad-bigint")
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"


def test_completed_temporary_keeps_invalid_basic_raw_CREATE_US_07_AC04(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    raw = {**VALID_BASIC, "title": "   ", "world_description": "  원문 그대로 \n"}

    data = stage(client, story, saved, raw, "temp-invalid-basic")

    assert data["working_copy_saved_at"] is not None
    assert staged(data) == {key: raw[key] for key in ORIGINAL_BASIC}
    # 다시 열면 임시 저장한 기본 정보가 원문 그대로 나온다.
    reopened = client.get(url(story)).json()
    assert staged(reopened) == {key: raw[key] for key in ORIGINAL_BASIC}
    # 이용자가 보는 저장본은 그대로다.
    assert committed_basic(client, story) == ORIGINAL_BASIC
    assert working_copy_rows(dsn, story) == 1


def test_unrelated_temporary_and_auto_save_keep_staged_basic_CREATE_US_07_AC07(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    stage(client, story, saved, VALID_BASIC, "temp-basic")

    # 기본 정보 없이 2번 문체만 고친 임시 저장
    edit = current_payload(saved)
    edit["expression"]["custom_style"] = "고치는 중인 문체"
    temporary = put(client, story, {**edit, "kind": "temporary"}, "temp-without-basic")
    assert temporary.status_code == 200, temporary.text
    assert staged(temporary.json()) == {key: VALID_BASIC[key] for key in ORIGINAL_BASIC}

    # 기본 정보 없이 보낸 자동 저장(언제나 임시 저장)
    edit["expression"]["custom_style"] = "자동 저장 문체"
    auto = put(client, story, {**edit, "mode": "auto"}, "auto-without-basic")
    assert auto.status_code == 200, auto.text
    assert staged(auto.json()) == {key: VALID_BASIC[key] for key in ORIGINAL_BASIC}

    reopened = client.get(url(story)).json()
    assert reopened["expression"]["custom_style"] == "자동 저장 문체"
    assert staged(reopened) == {key: VALID_BASIC[key] for key in ORIGINAL_BASIC}
    assert committed_basic(client, story) == ORIGINAL_BASIC


def test_save_without_basic_applies_latest_staged_basic_CREATE_US_07_AC07(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    stage(client, story, saved, {**VALID_BASIC, "title": "먼저 임시 제목"}, "temp-basic-1")
    stage(client, story, saved, VALID_BASIC, "temp-basic-2")
    # 그 사이 기본 정보 없는 임시 저장이 있어도 가장 최근 기본 정보가 남는다.
    plain = put(client, story, {**current_payload(saved), "kind": "temporary"}, "temp-plain")
    assert plain.status_code == 200, plain.text

    committed = put(client, story, current_payload(saved), "commit-staged-basic")

    assert committed.status_code == 200, committed.text
    assert committed.json()["working_copy_saved_at"] is None
    assert committed_basic(client, story) == {key: VALID_BASIC[key] for key in ORIGINAL_BASIC}
    assert working_copy_rows(dsn, story) == 0


def test_explicit_basic_on_save_takes_priority_over_staged_CREATE_US_07_AC07(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    stage(client, story, saved, VALID_BASIC, "temp-basic")
    kept = character_id(dsn, story)
    final = {
        "title": "최종 제목",
        "subtitle": "최종 소개",
        "world_description": "최종 세계관",
        "tags": ["로맨스"],
        "characters": [{"id": kept, "name": "하린", "description": "고친 상세"}],
    }

    committed = put(client, story, {**current_payload(saved), "basic": final}, "commit-explicit")

    assert committed.status_code == 200, committed.text
    assert committed.json()["working_copy_saved_at"] is None
    assert committed_basic(client, story) == {key: final[key] for key in ORIGINAL_BASIC}
    characters = client.get(basic_url(story)).json()["characters"]
    assert [(c["id"], c["name"], c["description"]) for c in characters] == [
        (kept, "하린", "고친 상세")
    ]
    assert working_copy_rows(dsn, story) == 0


def test_rejected_save_for_invalid_intro_keeps_staged_basic_CREATE_US_07_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    stage(client, story, saved, VALID_BASIC, "temp-basic")

    rejected = put(client, story, {**current_payload(saved), "intros": []}, "commit-no-intro")

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INTRO_REQUIRED"
    # 1번 기본 정보도 반영하지 않는다(통합 저장 전체 롤백).
    assert committed_basic(client, story) == ORIGINAL_BASIC
    assert working_copy_rows(dsn, story) == 1
    assert staged(client.get(url(story)).json()) == {
        key: VALID_BASIC[key] for key in ORIGINAL_BASIC
    }


def test_rejected_save_for_staged_invalid_basic_keeps_it_raw_CREATE_US_07_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    raw = {**VALID_BASIC, "title": "   "}
    stage(client, story, saved, raw, "temp-invalid-basic")

    rejected = put(client, story, current_payload(saved), "commit-staged-invalid")

    assert rejected.status_code == 422
    issues = {(i["field"], i["code"]) for i in rejected.json()["issues"]}
    assert ("title", "BASIC_SETTINGS_INCOMPLETE") in issues
    assert committed_basic(client, story) == ORIGINAL_BASIC
    assert working_copy_rows(dsn, story) == 1
    assert staged(client.get(url(story)).json()) == {key: raw[key] for key in ORIGINAL_BASIC}


def test_rejected_save_for_explicit_invalid_basic_keeps_staged_CREATE_US_07_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    stage(client, story, saved, VALID_BASIC, "temp-basic")
    invalid = {**VALID_BASIC, "title": ""}

    rejected = put(
        client, story, {**current_payload(saved), "basic": invalid}, "commit-explicit-invalid"
    )

    assert rejected.status_code == 422
    issues = {(i["field"], i["code"]) for i in rejected.json()["issues"]}
    assert ("title", "BASIC_SETTINGS_INCOMPLETE") in issues
    assert committed_basic(client, story) == ORIGINAL_BASIC
    assert working_copy_rows(dsn, story) == 1
    # 거절된 요청의 기본 정보가 아니라 이전에 임시 저장한 기본 정보가 남는다.
    assert staged(client.get(url(story)).json()) == {
        key: VALID_BASIC[key] for key in ORIGINAL_BASIC
    }


def test_removing_last_character_rejects_final_and_keeps_staged_CREATE_US_07_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    stage(client, story, saved, {**VALID_BASIC, "characters": []}, "temp-remove")

    result = put(client, story, current_payload(saved), "final-remove")

    assert result.status_code == 422
    assert result.json()["code"] == "BASIC_SETTINGS_INCOMPLETE"
    assert client.get(basic_url(story)).json()["characters"][0]["id"] == character_id(dsn, story)
    assert client.get(url(story)).json()["basic"]["characters"] == []


def test_tag_errors_point_to_submitted_order_CREATE_US_07_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO tags(name) VALUES (%s), (%s)", ("romance", "Romance"))
    basic = {**VALID_BASIC, "tags": ["Romance", "romance"]}
    stage(client, story, saved, basic, "temp-duplicate-tags")

    result = put(client, story, current_payload(saved), "final-duplicate-tags")

    assert result.status_code == 422
    duplicates = [i for i in result.json()["issues"] if i["code"] == "TAG_DUPLICATE"]
    assert [(i["field"], i["value"]) for i in duplicates] == [("tags[1]", "romance")]
    assert client.get(url(story)).json()["basic"]["tags"] == basic["tags"]


def test_tag_display_marker_does_not_count_toward_limit_CREATE_US_07_AC07(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    bare = "가나다라마바사아자차"
    basic = {**VALID_BASIC, "tags": [f"#{bare}"]}
    stage(client, story, saved, basic, "temp-prefixed-tag")
    assert client.get(url(story)).json()["basic"]["tags"] == basic["tags"]

    result = put(client, story, current_payload(saved), "final-prefixed-tag")

    assert result.status_code == 200, result.text
    assert client.get(basic_url(story)).json()["tags"] == [bare]


# ---------------------------------------------------------------- CREATE-US-07-AC09 자동 저장 확정
# 비공개·공개 스토리의 공통 자동 저장(preserve_scene) 응답은 그 앞에 다른 임시 저장본이 없었을
# 때(또는 앞의 임시 저장본이 아직 바뀌지 않은 확정 가능 자동 저장일 때)만
# `finalize_auto_request_id`로 자기 요청키를 돌려준다. 「저장」이 이 값을 보내면 서버는 그 자동
# 저장 뒤로 저장본·임시 저장본이 바뀌지 않았을 때만 임시 저장본 전체를 반영하고, 바뀌었으면
# 아무것도 쓰지 않고 409 CREATION_AUTOSAVE_CHANGED로 거절한다.


def autosave(client, story, saved, key, **extra):
    payload = {
        **current_payload(saved),
        "kind": "temporary",
        "mode": "auto",
        "preserve_scene": True,
        **extra,
    }
    result = put(client, story, payload, key)
    assert result.status_code == 200, result.text
    return result.json()


def finalize_payload(saved, auto_key):
    return {**current_payload(saved), "preserve_scene": True, "finalize_auto_request_id": auto_key}


def photos(client, story):
    result = client.get(f"{basic_url(story)}/images")
    assert result.status_code == 200, result.text
    return result.json()


def test_photo_only_autosave_can_be_finalized_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    cover = image(dsn)
    key = "photo-only-autosave"
    result = client.put(
        f"{basic_url(story)}/images",
        json={"kind": "temporary", "mode": "auto", "story_image_ids": [cover]},
        headers={"Idempotency-Key": key},
    )
    assert result.status_code == 200, result.text
    assert result.json()["finalize_auto_request_id"] == key
    assert photos(client, story)["finalize_auto_request_id"] is None
    final = put(client, story, finalize_payload(saved, key), "photo-only-final")
    assert final.status_code == 200, final.text
    assert final.json()["images_working_copy_saved_at"] is None
    assert photos(client, story)["story_images"][0]["image_id"] == cover
    replay = put(client, story, finalize_payload(saved, key), "photo-only-final")
    assert replay.json() == final.json()


def test_finalize_autosave_commits_mixed_basic_and_cover_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    cover = image(dsn)
    auto = autosave(
        client,
        story,
        saved,
        "auto-mixed-final",
        basic=VALID_BASIC,
        images={"story_image_ids": [cover]},
    )
    assert auto["finalize_auto_request_id"] == "auto-mixed-final"
    assert committed_basic(client, story) == ORIGINAL_BASIC

    final = put(client, story, finalize_payload(saved, "auto-mixed-final"), "final-mixed")

    assert final.status_code == 200, final.text
    assert final.json()["finalize_auto_request_id"] is None
    assert final.json()["save_state"] == "private"
    assert committed_basic(client, story)["title"] == VALID_BASIC["title"]
    shown_photos = photos(client, story)
    assert [i["image_id"] for i in shown_photos["story_images"]] == [cover]
    assert shown_photos["working_copy_saved_at"] is None
    reopened = client.get(url(story)).json()
    assert reopened["working_copy_saved_at"] is None
    assert reopened["images_working_copy_saved_at"] is None
    assert reopened["basic"] is None
    assert working_copy_rows(dsn, story) == 0


def test_autosave_finalize_eligibility_follows_autosave_only_staging_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    first = autosave(client, story, saved, "auto-chain-1", basic=VALID_BASIC)
    second = autosave(
        client, story, saved, "auto-chain-2", images={"story_image_ids": [image(dsn)]}
    )
    assert first["finalize_auto_request_id"] == "auto-chain-1"
    # 앞의 임시 저장본이 바뀌지 않은 확정 가능 자동 저장뿐이면 이어서도 확정할 수 있다.
    assert second["finalize_auto_request_id"] == "auto-chain-2"

    stage(client, story, saved, {**VALID_BASIC, "title": "직접 임시 저장"}, "manual-temp")
    third = autosave(client, story, saved, "auto-chain-3", basic=VALID_BASIC)
    # 직접 임시 저장한 내용이 섞인 임시 저장본은 자동 저장 확정으로 반영하지 않는다.
    assert third["finalize_auto_request_id"] is None

    for key in ("auto-chain-2", "auto-chain-3"):
        rejected = put(client, story, finalize_payload(saved, key), f"final-{key}")
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "CREATION_AUTOSAVE_CHANGED"
    assert committed_basic(client, story) == ORIGINAL_BASIC


def test_finalize_replay_after_later_edit_keeps_later_edit_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    autosave(client, story, saved, "auto-replay", basic=VALID_BASIC)
    payload = finalize_payload(saved, "auto-replay")
    first = put(client, story, payload, "final-replay")
    assert first.status_code == 200, first.text

    later = stage(client, story, saved, {**VALID_BASIC, "title": "나중 편집"}, "later-edit")
    replay = put(client, story, payload, "final-replay")

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert committed_basic(client, story)["title"] == VALID_BASIC["title"]
    reopened = client.get(url(story)).json()
    assert reopened["basic"]["title"] == "나중 편집"
    assert reopened["working_copy_saved_at"] == later["working_copy_saved_at"]
    # 이미 확정한 자동 저장을 새 요청키로 다시 확정하려 하면 나중 편집을 덮지 않고 거절한다.
    again = put(client, story, payload, "final-replay-new-key")
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "CREATION_AUTOSAVE_CHANGED"
    assert client.get(url(story)).json() == reopened


@pytest.mark.parametrize("change", ["scene", "image"])
def test_finalize_rejects_intervening_change_and_keeps_both_views_CREATE_US_07_AC09(
    context, change
):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    cover, other = image(dsn), image(dsn)
    autosave(
        client,
        story,
        saved,
        "auto-intervened",
        basic=VALID_BASIC,
        images={"story_image_ids": [cover]},
    )
    if change == "scene":
        edited = current_payload(saved)
        edited["expression"]["custom_style"] = "사이에 고친 문체"
        result = put(client, story, {**edited, "kind": "temporary"}, "scene-edit")
    else:
        result = client.put(
            f"{basic_url(story)}/images",
            json={"story_image_ids": [other], "kind": "temporary"},
            headers={"Idempotency-Key": "image-edit"},
        )
    assert result.status_code == 200, result.text
    pending_before = client.get(url(story)).json()
    photos_before = photos(client, story)

    rejected = put(client, story, finalize_payload(saved, "auto-intervened"), "final-intervened")

    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "CREATION_AUTOSAVE_CHANGED"
    # 이용자가 보는 저장본도, 편집 화면의 임시 저장본도 그대로다.
    assert committed_basic(client, story) == ORIGINAL_BASIC
    assert client.get(url(story)).json() == pending_before
    assert photos(client, story) == photos_before
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM story_images WHERE story_id = %s", (story["id"],)
            ).fetchone()
            is None
        )


def test_finalize_field_shape_and_hash_compatibility_CREATE_US_07_AC09():
    base = {**body("private"), "preserve_scene": True}
    plain = CreationSaveRequest.model_validate(base)
    explicit_null = CreationSaveRequest.model_validate({**base, "finalize_auto_request_id": None})
    # 생략·null은 기존 요청 해시를 바꾸지 않는다.
    assert request_digest(1, plain) == request_digest(1, explicit_null)
    finalize = CreationSaveRequest.model_validate({**base, "finalize_auto_request_id": "auto-1"})
    assert request_digest(1, finalize) != request_digest(1, plain)
    for extra in (
        {"kind": "temporary"},
        {"mode": "auto", "kind": "temporary"},
        {"preserve_scene": False},
        {"basic": VALID_BASIC},
        {"presets": []},
        {"images": {"story_image_ids": []}},
        {"finalize_auto_request_id": ""},
        {"finalize_auto_request_id": "x" * 121},
        {"finalize_auto_request_id": "bad key"},
    ):
        with pytest.raises(ValidationError):
            CreationSaveRequest.model_validate(
                {**base, "finalize_auto_request_id": "auto-1", **extra}
            )
