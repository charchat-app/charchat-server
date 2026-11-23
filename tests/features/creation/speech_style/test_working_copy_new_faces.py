"""CREATE-US-07-AC09 새 캐릭터 얼굴 사진을 임시 저장·자동 저장에 함께 두는 통합검사.

계약(개발자 지정):
- 비공개·공개 스토리의 `preserve_scene` 임시 저장(수동·자동)은 `images.characters[].basic_index`를
  받는다. 미등록 스토리는 임시 저장도 본 테이블에 바로 써 새 ID가 일찍 생기므로 거절한다.
- `basic_index` 사진은 임시 저장한 `basic`과 짝을 지어 공통 문서(creation JSON)에만 둔다.
  6번 사진 목록(저장본·사진 임시 저장본)은 바꾸지 않는다. 스키마 마이그레이션은 없다.
- `basic`을 다시 보내면서 `basic_index` 사진을 보내지 않으면 이전 사진을 비우고, `basic`을 빼면
  이전 사진을 유지한다. 소유·GIF를 확인하고, 보호 연결(`story_working_copy_images`)을 맞춘다.
- 조회·저장 응답은 `staged_character_images: [{basic_index, images: [사진 API와 같은 항목]}]`.
- `basic`을 뺀 「저장」(자동 저장 확정 포함)은 임시 저장한 새 캐릭터 사진을 명단 정리 뒤 새 행의
  순서대로 연결하고 완성 검사 전에 반영한다. 직접 보낸 `basic`·`images`가 우선한다.
- 실패하면 전체를 되돌리고 임시 저장본·보호 연결을 그대로 둔다.

`TEST_DATABASE_URL`(폐기 가능한 전용 테스트 DB)이 없으면 DB 검사는 건너뛴다.
건너뛴 결과는 검증 완료가 아니다.
"""

import psycopg
import pytest
from pydantic import ValidationError

from app.features.creation.save_visibility.schemas import CreationSaveRequest
from tests.features.creation.speech_style.test_common_save_api import (
    body,
    complete,
    current_payload,
    put,
    url,
)
from tests.features.creation.speech_style.test_intro_common_save import add_character
from tests.features.creation.speech_style.test_working_copy_basic import (
    VALID_BASIC,
    autosave,
    basic_url,
    character_id,
    finalize_payload,
    photos,
)
from tests.features.creation.speech_style.test_working_copy_presets import OTHER_ID, image
from tests.features.creation.speech_style.test_working_copy_save import (
    publish,
    working_copy_rows,
)

NEW_NAMES = ("서연", "도윤")


def roster(dsn, story, names=NEW_NAMES):
    """기존 캐릭터 1명 뒤에 새 캐릭터(id null)를 붙인 기본 정보 캐릭터 목록."""
    kept = character_id(dsn, story)
    return [{"id": kept, "name": "하린", "description": "원래 상세"}] + [
        {"id": None, "name": name, "description": f"{name} 상세"} for name in names
    ]


def face_entries(faces):
    return [{"basic_index": index, "image_ids": ids} for index, ids in faces.items()]


def stage_faces(client, story, saved, key, characters, faces, **extra):
    """새 캐릭터 사진을 담아 `preserve_scene` 임시 저장한다. 응답(JSON)을 돌려준다."""
    payload = {
        **current_payload(saved),
        "kind": "temporary",
        "preserve_scene": True,
        "basic": {**VALID_BASIC, "characters": characters},
        **extra,
    }
    if faces:
        payload["images"] = {"characters": face_entries(faces)}
    result = put(client, story, payload, key)
    assert result.status_code == 200, result.text
    return result.json()


def staged_ids(response_json):
    """응답의 `staged_character_images`를 {basic_index: [image_id...]}로 줄인다."""
    return {
        entry["basic_index"]: [item["image_id"] for item in entry["images"]]
        for entry in response_json["staged_character_images"]
    }


def protected(dsn, story):
    """임시 저장본이 보호 연결로 쥔 이미지 ID."""
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT image_id FROM story_working_copy_images WHERE story_id=%s", (story["id"],)
        ).fetchall()
    return {row[0] for row in rows}


def orphaned(dsn, image_id):
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT orphaned_at FROM images WHERE id=%s", (image_id,)
        ).fetchone()
    return row[0] is not None


def committed_people(client, story):
    result = client.get(basic_url(story))
    assert result.status_code == 200, result.text
    return [(c["id"], c["name"]) for c in result.json()["characters"]]


def final_without_basic(client, story, saved, key, **extra):
    return put(client, story, {**current_payload(saved), "preserve_scene": True, **extra}, key)


# ---------------------------------------------------------------- 요청 모양 (DB 없음)


def test_temporary_preserve_scene_accepts_partial_basic_index_CREATE_US_07_AC09():
    characters = [
        {"id": 1, "name": "하린", "description": "상세"},
        {"id": None, "name": "서연", "description": "상세"},
        {"id": None, "name": "도윤", "description": "상세"},
    ]
    payload = {
        **body("private"),
        "preserve_scene": True,
        "basic": {**VALID_BASIC, "characters": characters},
        "images": {"characters": [{"basic_index": 1, "image_ids": [7]}]},
    }
    for mode in ({"kind": "temporary"}, {"kind": "temporary", "mode": "auto"}):
        # 임시 저장은 일부 새 캐릭터만 사진을 담아도 된다(완성 검사는 「저장」에서 한다).
        CreationSaveRequest.model_validate({**payload, **mode})
    # preserve_scene 없는 임시 저장은 지금처럼 거절한다.
    with pytest.raises(ValidationError):
        CreationSaveRequest.model_validate(
            {**payload, "kind": "temporary", "preserve_scene": False}
        )
    # 「저장」은 새 캐릭터마다 목록이 있어야 한다(기존 규칙 유지).
    with pytest.raises(ValidationError):
        CreationSaveRequest.model_validate(payload)
    # 기존 캐릭터를 가리키는 basic_index는 임시 저장에서도 거절한다.
    with pytest.raises(ValidationError):
        CreationSaveRequest.model_validate(
            {
                **payload,
                "kind": "temporary",
                "images": {"characters": [{"basic_index": 0, "image_ids": [7]}]},
            }
        )


# ---------------------------------------------------------------- 임시 저장·다시 열기


def test_two_new_faces_staged_then_committed_by_final_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    kept = character_id(dsn, story)
    first_face, second_face = image(dsn), image(dsn)
    photos_before = photos(client, story)

    staged = stage_faces(
        client, story, saved, "stage-two", roster(dsn, story), {1: [first_face], 2: [second_face]}
    )

    assert staged_ids(staged) == {1: [first_face], 2: [second_face]}
    reopened = client.get(url(story)).json()
    assert reopened["staged_character_images"] == staged["staged_character_images"]
    # 저장본 명단·6번 사진 목록(사진 임시 저장본 포함)은 그대로다.
    assert committed_people(client, story) == [(kept, "하린")]
    assert photos(client, story) == photos_before
    assert reopened["images_working_copy_saved_at"] is None
    # 임시 저장본이 두 사진을 보호한다(정리 작업 대상이 아니다).
    assert protected(dsn, story) == {first_face, second_face}
    assert not orphaned(dsn, first_face) and not orphaned(dsn, second_face)

    final = final_without_basic(client, story, saved, "final-two")

    assert final.status_code == 200, final.text
    assert final.json()["staged_character_images"] == []
    people = committed_people(client, story)
    assert [name for _, name in people] == ["하린", *NEW_NAMES]
    shown = photos(client, story)["characters"]
    assert [[i["image_id"] for i in c["images"]] for c in shown[1:]] == [
        [first_face],
        [second_face],
    ]
    # 임시 저장 응답의 사진 항목은 사진 API 항목과 같은 모양·값이다.
    assert staged["staged_character_images"][0]["images"] == shown[1]["images"]
    assert staged["staged_character_images"][1]["images"] == shown[2]["images"]
    assert working_copy_rows(dsn, story) == 0
    assert protected(dsn, story) == set()
    assert not orphaned(dsn, first_face) and not orphaned(dsn, second_face)


def test_partial_new_faces_stage_but_final_rejects_and_keeps_pending_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    kept = character_id(dsn, story)
    face = image(dsn)
    staged = stage_faces(client, story, saved, "stage-partial", roster(dsn, story), {2: [face]})
    assert staged_ids(staged) == {2: [face]}

    rejected = final_without_basic(client, story, saved, "final-partial")

    assert rejected.status_code == 422, rejected.text
    issues = {(i["field"], i["code"]) for i in rejected.json()["issues"]}
    assert ("characters[1].images", "CHARACTER_IMAGE_REQUIRED") in issues
    assert committed_people(client, story) == [(kept, "하린")]
    reopened = client.get(url(story)).json()
    assert reopened["working_copy_saved_at"] == staged["working_copy_saved_at"]
    assert reopened["staged_character_images"] == staged["staged_character_images"]
    assert protected(dsn, story) == {face}


def test_omitted_basic_keeps_faces_and_replaced_basic_clears_them_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    face = image(dsn)
    stage_faces(
        client, story, saved, "stage-keep", roster(dsn, story), {1: [face], 2: [image(dsn)]}
    )

    # 기본 정보 없이 다른 영역만 고친 임시 저장·자동 저장은 사진을 유지한다.
    edit = current_payload(saved)
    edit["expression"]["custom_style"] = "고치는 중"
    kept = put(client, story, {**edit, "kind": "temporary"}, "temp-no-basic")
    assert kept.status_code == 200, kept.text
    assert staged_ids(kept.json())[1] == [face]
    auto = autosave(client, story, saved, "auto-no-basic")
    assert staged_ids(auto)[1] == [face]
    assert staged_ids(client.get(url(story)).json())[1] == [face]

    # 기본 정보를 다시 보내면서 basic_index 사진을 보내지 않으면 이전 사진은 비운다.
    replaced = stage_faces(client, story, saved, "stage-replace", roster(dsn, story), {})

    assert replaced["staged_character_images"] == []
    assert client.get(url(story)).json()["staged_character_images"] == []
    assert protected(dsn, story) == set()
    assert orphaned(dsn, face)


def test_restaged_basic_replaces_faces_exactly_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    old, new = image(dsn), image(dsn)
    stage_faces(client, story, saved, "stage-old", roster(dsn, story, ("서연",)), {1: [old]})

    restaged = stage_faces(
        client, story, saved, "stage-new", roster(dsn, story, ("서연",)), {1: [new]}
    )

    assert staged_ids(restaged) == {1: [new]}
    assert protected(dsn, story) == {new}
    assert orphaned(dsn, old) and not orphaned(dsn, new)


# ---------------------------------------------------------------- 거절


def test_draft_story_rejects_staged_new_faces_before_any_write_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    complete(dsn, story)
    face = image(dsn)
    result = put(
        client,
        story,
        {
            **body(),
            "kind": "temporary",
            "preserve_scene": True,
            "basic": {**VALID_BASIC, "characters": roster(dsn, story, ("서연",))},
            "images": {"characters": [{"basic_index": 1, "image_ids": [face]}]},
        },
        "draft-new-face",
    )

    assert result.status_code == 422, result.text
    assert result.json()["code"] == "CHARACTER_IMAGE_INDEX_UNSUPPORTED"
    assert [name for _, name in committed_people(client, story)] == ["하린"]
    assert working_copy_rows(dsn, story) == 0
    assert orphaned(dsn, face)


@pytest.mark.parametrize(
    ("kind", "code"), [("foreign", "IMAGE_NOT_FOUND"), ("gif", "IMAGE_GIF_NOT_ALLOWED")]
)
def test_staged_new_face_checks_owner_and_gif_CREATE_US_07_AC09(context, kind, code):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    good = image(dsn)
    first = stage_faces(
        client, story, saved, "stage-good", roster(dsn, story, ("서연",)), {1: [good]}
    )
    bad = image(dsn, owner_id=OTHER_ID) if kind == "foreign" else image(dsn, mime="image/gif")

    result = put(
        client,
        story,
        {
            **current_payload(saved),
            "kind": "temporary",
            "preserve_scene": True,
            "basic": {**VALID_BASIC, "characters": roster(dsn, story, ("서연",))},
            "images": {"characters": [{"basic_index": 1, "image_ids": [bad]}]},
        },
        f"stage-{kind}",
    )

    assert result.status_code == 422, result.text
    assert result.json()["code"] == code
    reopened = client.get(url(story)).json()
    assert reopened["working_copy_saved_at"] == first["working_copy_saved_at"]
    assert reopened["staged_character_images"] == first["staged_character_images"]
    assert protected(dsn, story) == {good}


# ---------------------------------------------------------------- 「저장」


def test_final_maps_faces_by_new_row_order_after_later_character_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    first_face, second_face = image(dsn), image(dsn)
    stage_faces(
        client,
        story,
        saved,
        "stage-before-later",
        roster(dsn, story),
        {1: [first_face], 2: [second_face]},
    )
    # 임시 저장 뒤 다른 화면이 캐릭터를 추가했다(임시 목록에는 없음 → 목록 끝에 남는다).
    later = add_character(dsn, story, "나중", 1)

    final = final_without_basic(client, story, saved, "final-after-later")

    assert final.status_code == 200, final.text
    people = committed_people(client, story)
    assert [name for _, name in people] == ["하린", *NEW_NAMES, "나중"]
    assert people[3][0] == later
    shown = photos(client, story)["characters"]
    assert [i["image_id"] for i in shown[1]["images"]] == [first_face]
    assert [i["image_id"] for i in shown[2]["images"]] == [second_face]


def test_final_combines_staged_faces_with_explicit_images_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    kept = character_id(dsn, story)
    new_face, kept_face, cover = image(dsn), image(dsn), image(dsn)
    stage_faces(
        client, story, saved, "stage-combined", roster(dsn, story, ("서연",)), {1: [new_face]}
    )

    final = final_without_basic(
        client,
        story,
        saved,
        "final-combined",
        images={
            "story_image_ids": [cover],
            "characters": [{"character_id": kept, "image_ids": [kept_face]}],
        },
    )

    assert final.status_code == 200, final.text
    shown = photos(client, story)
    assert [i["image_id"] for i in shown["story_images"]] == [cover]
    assert [[i["image_id"] for i in c["images"]] for c in shown["characters"]] == [
        [kept_face],
        [new_face],
    ]
    assert working_copy_rows(dsn, story) == 0


def test_explicit_basic_on_final_drops_staged_faces_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    kept = character_id(dsn, story)
    face = image(dsn)
    stage_faces(client, story, saved, "stage-explicit", roster(dsn, story, ("서연",)), {1: [face]})

    final = final_without_basic(
        client,
        story,
        saved,
        "final-explicit",
        basic={**VALID_BASIC, "characters": [{"id": kept, "name": "하린", "description": "상세"}]},
    )

    assert final.status_code == 200, final.text
    assert committed_people(client, story) == [(kept, "하린")]
    assert working_copy_rows(dsn, story) == 0
    # 임시 저장 사진은 쓰지 않았으므로 보호가 끊기고 정리 대상이 된다.
    assert protected(dsn, story) == set()
    assert orphaned(dsn, face)


def test_failed_final_rolls_back_new_rows_and_keeps_faces_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    kept = character_id(dsn, story)
    face = image(dsn)
    staged = stage_faces(
        client, story, saved, "stage-rollback", roster(dsn, story, ("서연",)), {1: [face]}
    )

    rejected = put(
        client,
        story,
        {**current_payload(saved), "intros": []},
        "final-rollback",
    )

    assert rejected.status_code == 422, rejected.text
    assert committed_people(client, story) == [(kept, "하린")]
    reopened = client.get(url(story)).json()
    assert reopened["staged_character_images"] == staged["staged_character_images"]
    assert protected(dsn, story) == {face}
    assert not orphaned(dsn, face)


def test_autosave_with_new_faces_can_be_finalized_CREATE_US_07_AC09(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    face = image(dsn)
    auto = autosave(
        client,
        story,
        saved,
        "auto-new-face",
        basic={**VALID_BASIC, "characters": roster(dsn, story, ("서연",))},
        images={"characters": [{"basic_index": 1, "image_ids": [face]}]},
    )
    assert auto["finalize_auto_request_id"] == "auto-new-face"
    assert staged_ids(auto) == {1: [face]}

    final = put(client, story, finalize_payload(saved, "auto-new-face"), "final-auto-new-face")

    assert final.status_code == 200, final.text
    shown = photos(client, story)["characters"]
    assert [c["name"] for c in shown] == ["하린", "서연"]
    assert [i["image_id"] for i in shown[1]["images"]] == [face]
    assert working_copy_rows(dsn, story) == 0
