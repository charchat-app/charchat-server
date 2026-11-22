"""CREATE-US-05 유저 프리셋을 7번 공통 저장(/creation)에 싣는 통합검사 (실제 HTTP·PostgreSQL).

대상 인수 조건(개발자 지정): CREATE-US-05-AC01, AC06, AC07, AC08 (서버 통합 조각).
- `presets`는 선택 항목이다. 빼거나 null이면 기존 값을 유지하고, `[]`는 명시적으로 모두 비운다.
- 비공개·공개 스토리의 임시 저장은 잘못된 프리셋도 원문 그대로 임시 저장본에만 둔다.
  이용자가 보는 저장본(story_user_presets)은 「저장」 전까지 바뀌지 않는다.
- 소유자 전용 GET /creation은 임시 저장한 프리셋이 있으면 그것(`presets_pending=true`)을,
  없으면 저장본 프리셋을 돌려준다.
- 「저장」은 보낸 값, 없으면 임시 저장한 값, 둘 다 없으면 저장본 프리셋을 검사한다.
  (이번 저장의 캐릭터 이름과의 중복, 필수 이미지, 사용할 수 없는 이미지)
  통과하면 다른 영역과 한 트랜잭션에 반영하고, 실패하면 아무것도 바꾸지 않는다.
- 이전 요청(프리셋 필드 없음)의 요청키 재전송은 같은 해시로 기록된 응답을 돌려준다.

`TEST_DATABASE_URL`(폐기 가능한 전용 테스트 DB)이 없으면 건너뛴다. 건너뛴 결과는 검증 완료가 아니다.
"""

import hashlib
import json
import secrets

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.features.creation.save_visibility.schemas import CreationSaveRequest
from app.features.creation.save_visibility.service import save_creation
from tests.features.creation.speech_style.conftest import OWNER_ID
from tests.features.creation.speech_style.test_common_save_api import (
    body,
    current_payload,
    put,
    url,
)
from tests.features.creation.speech_style.test_working_copy_save import (
    publish,
    working_copy_rows,
)

OTHER_ID = 200


def image(dsn, owner_id=OWNER_ID, mime="image/png"):
    """업로드만 된(연결 없는) 이미지 행."""
    ext = {"image/png": "png", "image/gif": "gif"}[mime]
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """INSERT INTO images(owner_id, storage_key, mime_type, byte_size, orphaned_at)
               VALUES (%s, %s, %s, 1, clock_timestamp()) RETURNING id""",
            (owner_id, f"{secrets.token_hex(16)}.{ext}", mime),
        ).fetchone()[0]


def insert_preset(dsn, story, name, image_id, position, description=None):
    """저장본 프리셋을 직접 넣는다(이전 저장 결과 또는 예전 데이터 흉내)."""
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """INSERT INTO story_user_presets(story_id, name, image_id, description, position)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (story["id"], name, image_id, description, position),
        ).fetchone()[0]


def canonical(dsn, story):
    """이용자가 보는 저장본 프리셋 (id, name, image_id, description) 편집 순서."""
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            """SELECT id, name, image_id, description FROM story_user_presets
               WHERE story_id=%s ORDER BY position, id""",
            (story["id"],),
        ).fetchall()
    return [tuple(row) for row in rows]


def working_copy_image_ids(dsn, story):
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT image_id FROM story_working_copy_images WHERE story_id=%s ORDER BY image_id",
            (story["id"],),
        ).fetchall()
    return [row[0] for row in rows]


def orphaned_at(dsn, image_id):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT orphaned_at FROM images WHERE id=%s", (image_id,)
        ).fetchone()[0]


def preset(name, image_id, preset_id=None, description=None):
    return {"id": preset_id, "name": name, "image_id": image_id, "description": description}


def shown(presets):
    """응답 프리셋에서 비교할 필드만 꺼낸다."""
    return [(p["id"], p["name"], p["image_id"], p["description"]) for p in presets]


def stage(client, story, saved, key, **extra):
    payload = {**current_payload(saved), "kind": "temporary", **extra}
    result = put(client, story, payload, key)
    assert result.status_code == 200, result.text
    return result.json()


def test_temporary_save_stages_exact_invalid_raw_presets_CREATE_US_05_AC06(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    owned = image(dsn)
    gif = image(dsn, mime="image/gif")
    foreign = image(dsn, owner_id=OTHER_ID)
    kept = insert_preset(dsn, story, "기존", owned, 0)
    before = canonical(dsn, story)
    raw = [
        preset(" 앞뒤 공백 ", owned, kept, "  상세 원문 \n"),
        preset(None, None),
        preset("하린", gif),  # 캐릭터 이름 중복 + GIF
        preset("{{user}}", foreign),  # 남의 이미지
        preset("가" * 1001, owned),  # 1000자 초과
        preset("여섯째", None, 987654),  # 이 스토리에 없는 id, 6개 초과
    ]

    data = stage(client, story, saved, "temp-raw-presets", presets=raw)

    assert data["working_copy_saved_at"] is not None
    assert data["presets_pending"] is True
    assert shown(data["presets"]) == [
        (p["id"], p["name"], p["image_id"], p["description"]) for p in raw
    ]
    reopened = client.get(url(story)).json()
    assert reopened["presets_pending"] is True
    assert shown(reopened["presets"]) == shown(data["presets"])
    # 이용자가 보는 저장본은 그대로다(앞당겨 반영하지 않는다).
    assert canonical(dsn, story) == before
    # 임시 저장본이 쓰는 내 이미지는 정리되지 않게 연결하고, 남의 이미지는 연결하지 않는다.
    assert working_copy_image_ids(dsn, story) == sorted([owned, gif])
    assert orphaned_at(dsn, gif) is None
    assert orphaned_at(dsn, foreign) is not None


def test_temporary_and_auto_save_without_presets_keep_staged_CREATE_US_05_AC06(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    staged = [preset("민수", image(dsn))]
    stage(client, story, saved, "temp-presets", presets=staged)

    plain = stage(client, story, saved, "temp-plain")  # 필드 생략
    nulled = stage(client, story, saved, "temp-null", presets=None)  # null도 생략과 같다
    auto = put(client, story, {**current_payload(saved), "mode": "auto"}, "auto-plain")
    assert auto.status_code == 200, auto.text

    for data in (plain, nulled, auto.json(), client.get(url(story)).json()):
        assert data["presets_pending"] is True
        assert shown(data["presets"]) == [(None, "민수", staged[0]["image_id"], None)]
    assert canonical(dsn, story) == []


def test_temporary_empty_list_stages_explicit_clear_CREATE_US_05_AC01(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    owned = image(dsn)
    kept = insert_preset(dsn, story, "기존", owned, 0)

    data = stage(client, story, saved, "temp-clear", presets=[])

    assert data["presets_pending"] is True
    assert data["presets"] == []
    assert canonical(dsn, story) == [(kept, "기존", owned, None)]

    committed = put(client, story, current_payload(saved), "commit-clear")
    assert committed.status_code == 200, committed.text
    assert committed.json()["presets"] == []
    assert committed.json()["presets_pending"] is False
    assert canonical(dsn, story) == []
    assert working_copy_rows(dsn, story) == 0


def test_temporary_image_save_keeps_staged_preset_image_link_CREATE_US_05_AC06(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    owned = image(dsn)
    stage(client, story, saved, "temp-presets", presets=[preset("민수", owned)])

    # 6번 이미지 목록만 임시 저장해도 2~4·5번 임시 저장본이 쓰는 사진 연결은 남아야 한다.
    images = client.put(
        f"/api/v1/creation/stories/{story['id']}/images",
        json={"story_image_ids": [], "characters": [], "kind": "temporary"},
        headers={"Idempotency-Key": "temp-images"},
    )
    assert images.status_code == 200, images.text
    assert owned in working_copy_image_ids(dsn, story)
    assert orphaned_at(dsn, owned) is None


def test_get_creation_resolves_canonical_then_pending_owner_only_CREATE_US_05_AC01(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    owned = image(dsn)
    kept = insert_preset(dsn, story, "기존", owned, 0, "상세")

    first = client.get(url(story)).json()
    assert first["presets_pending"] is False
    assert shown(first["presets"]) == [(kept, "기존", owned, "상세")]

    stage(client, story, saved, "temp-presets", presets=[preset("고친 이름", owned, kept)])
    second = client.get(url(story)).json()
    assert second["presets_pending"] is True
    assert shown(second["presets"]) == [(kept, "고친 이름", owned, None)]

    # 다른 사람은 임시 저장본·저장본 어느 것도 볼 수 없다.
    other = client.get(url(story), headers={"X-User-Id": str(OTHER_ID)})
    assert other.status_code == 404
    assert other.json()["code"] == "STORY_NOT_FOUND"


def test_save_without_presets_applies_staged_with_ids_kept_CREATE_US_05_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    old_image, new_image = image(dsn), image(dsn)
    kept = insert_preset(dsn, story, "기존", old_image, 0)
    dropped = insert_preset(dsn, story, "지울 것", old_image, 1)
    staged = [preset("새 프리셋", new_image), preset("{{user}}", old_image, kept, "고친 상세")]
    stage(client, story, saved, "temp-presets", presets=staged)

    committed = put(client, story, current_payload(saved), "commit-staged")

    assert committed.status_code == 200, committed.text
    data = committed.json()
    assert data["working_copy_saved_at"] is None
    assert data["presets_pending"] is False
    rows = canonical(dsn, story)
    assert [row[1:] for row in rows] == [
        ("새 프리셋", new_image, None),
        ("{{user}}", old_image, "고친 상세"),
    ]
    assert rows[1][0] == kept  # 기존 프리셋 id 유지
    assert dropped not in [row[0] for row in rows]
    assert shown(data["presets"]) == rows
    assert working_copy_rows(dsn, story) == 0
    assert working_copy_image_ids(dsn, story) == []
    assert orphaned_at(dsn, new_image) is None


def test_explicit_presets_on_save_take_priority_over_staged_CREATE_US_05_AC01(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    staged_image, final_image = image(dsn), image(dsn)
    stage(client, story, saved, "temp-presets", presets=[preset("임시", staged_image)])

    final = [preset("최종", final_image)]
    committed = put(client, story, {**current_payload(saved), "presets": final}, "commit-final")

    assert committed.status_code == 200, committed.text
    assert [row[1:] for row in canonical(dsn, story)] == [("최종", final_image, None)]
    assert working_copy_rows(dsn, story) == 0
    # 임시 저장본에서만 쓰던 사진은 연결이 끊겨 미사용 표시가 다시 시작된다.
    assert orphaned_at(dsn, staged_image) is not None


def test_save_with_omitted_presets_and_nothing_staged_keeps_canonical_CREATE_US_05_AC01(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    owned = image(dsn)
    kept = insert_preset(dsn, story, "기존", owned, 0)

    committed = put(client, story, current_payload(saved), "commit-omitted")

    assert committed.status_code == 200, committed.text
    assert canonical(dsn, story) == [(kept, "기존", owned, None)]
    assert shown(committed.json()["presets"]) == [(kept, "기존", owned, None)]


def test_unregistered_story_writes_presets_directly_CREATE_US_05_AC01(context):
    client, _, story, _, dsn = context
    owned = image(dsn)
    draft = {**body(), "kind": "temporary"}

    # 미등록 스토리는 임시 저장도 본 테이블에 바로 쓴다. 미완성 초안(이름·이미지 없음)도 보존한다.
    first = put(client, story, {**draft, "presets": [preset(None, None), preset("민수", owned)]})
    assert first.status_code == 200, first.text
    assert first.json()["presets_pending"] is False
    assert [row[1:] for row in canonical(dsn, story)] == [
        (None, None, None),
        ("민수", owned, None),
    ]
    assert working_copy_rows(dsn, story) == 0

    assert put(client, story, draft, "draft-omitted").status_code == 200
    assert len(canonical(dsn, story)) == 2
    assert put(client, story, {**draft, "presets": []}, "draft-clear").status_code == 200
    assert canonical(dsn, story) == []


def test_rejected_staged_preset_missing_image_changes_nothing_CREATE_US_05_AC07(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    owned = image(dsn)
    kept = insert_preset(dsn, story, "기존", owned, 0)
    stage(
        client,
        story,
        saved,
        "temp-invalid",
        presets=[preset("이미지 없음", None)],
    )

    rejected = put(client, story, current_payload(saved), "commit-invalid")

    assert rejected.status_code == 422
    issues = {(i["field"], i["code"]) for i in rejected.json()["issues"]}
    assert ("presets[0].image_id", "REQUIRED") in issues
    # 최종 저장에 실패하면 저장본과 다시 편집할 임시본을 모두 보존한다.
    assert canonical(dsn, story) == [(kept, "기존", owned, None)]
    assert working_copy_rows(dsn, story) == 1
    reopened = client.get(url(story)).json()
    assert shown(reopened["presets"]) == [(None, "이미지 없음", None, None)]


def test_persisted_presets_need_eligible_images_CREATE_US_05_AC07(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    # 예전 데이터처럼 저장본에 남은 이미지 누락·GIF·남의 이미지
    insert_preset(dsn, story, "이미지 없음", None, 0)
    insert_preset(dsn, story, "움짤", image(dsn, mime="image/gif"), 1)
    insert_preset(dsn, story, "남의 사진", image(dsn, owner_id=OTHER_ID), 2)
    before = client.get(url(story)).json()

    rejected = put(client, story, current_payload(saved, "public"), "commit-public")

    assert rejected.status_code == 422
    issues = {(i["field"], i["code"]) for i in rejected.json()["issues"]}
    assert {
        ("presets[0].image_id", "REQUIRED"),
        ("presets[1].image_id", "INVALID_PRESET_IMAGE"),
        ("presets[2].image_id", "INVALID_PRESET_IMAGE"),
    } <= issues
    assert client.get(url(story)).json() == before  # 공개 전환도 하지 않았다


def test_staged_foreign_image_is_rejected_and_staged_kept_CREATE_US_05_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    foreign = image(dsn, owner_id=OTHER_ID)
    stage(client, story, saved, "temp-foreign", presets=[preset("민수", foreign)])

    rejected = put(client, story, current_payload(saved), "commit-foreign")

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INVALID_PRESET_IMAGE"
    assert canonical(dsn, story) == []
    assert working_copy_rows(dsn, story) == 1
    assert shown(client.get(url(story)).json()["presets"]) == [(None, "민수", foreign, None)]


@pytest.mark.parametrize("kind", ["temporary", "save"])
def test_legacy_foreign_link_detaches_without_waiting_CREATE_US_05_AC06(context, kind):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    foreign = image(dsn, owner_id=OTHER_ID)
    stage(client, story, saved, "temp-foreign-legacy", presets=[preset("남의 사진", foreign)])
    with psycopg.connect(dsn) as setup:
        setup.execute(
            "INSERT INTO story_working_copy_images(story_id, image_id) VALUES (%s, %s)",
            (story["id"], foreign),
        )
    with psycopg.connect(dsn) as editor:
        editor.execute("SELECT id FROM images WHERE id=%s FOR UPDATE", (foreign,))
        # 잠금을 쥔 동안 저장과 연결 해제가 끝나야 한다.
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as writer:
            writer.execute("SET lock_timeout = '250ms'")
            request = CreationSaveRequest.model_validate(
                {**current_payload(saved), "presets": [], "kind": kind}
            )
            save_creation(writer, story["id"], OWNER_ID, "remove-foreign-link", request)
        assert working_copy_image_ids(dsn, story) == []


def test_valid_presets_reach_both_completed_states_CREATE_US_05_AC07(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    presets = [preset("{{user}}", image(dsn)), preset("{{user}}", image(dsn), description="x")]

    public = put(
        client, story, {**current_payload(saved, "public"), "presets": presets}, "commit-public"
    )

    assert public.status_code == 200, public.text
    assert public.json()["save_state"] == "public"
    assert len(canonical(dsn, story)) == 2


def test_old_request_without_presets_replays_recorded_response_CREATE_US_05_AC08(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    payload = current_payload(saved)
    first = put(client, story, payload, "old-key")
    assert first.status_code == 200, first.text

    # 프리셋 통합 전 형식: 요청 해시에 presets가 없고, 응답에도 presets 필드가 없다.
    old_hash = hashlib.sha256(
        json.dumps(
            {
                "operation": "creation-save-v4",
                "story_id": str(story["id"]),
                **CreationSaveRequest.model_validate(payload).model_dump(
                    mode="json", exclude={"basic", "presets", "images", "preserve_scene"}
                ),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT request_hash, response FROM creation_write_requests "
            "WHERE owner_id=%s AND request_id=%s",
            (OWNER_ID, "old-key"),
        ).fetchone()
        assert row[0] == old_hash
        old_response = {k: v for k, v in row[1].items() if k not in ("presets", "presets_pending")}
        connection.execute(
            "UPDATE creation_write_requests SET response=%s WHERE owner_id=%s AND request_id=%s",
            (Jsonb(old_response), OWNER_ID, "old-key"),
        )
    insert_preset(dsn, story, "나중 프리셋", image(dsn), 0)

    replay = put(client, story, payload, "old-key")
    assert replay.status_code == 200, replay.text
    assert replay.json()["presets"] == []  # 기록한 응답을 그대로(새 필드는 기본값)
    assert replay.json()["presets_pending"] is False

    # null은 생략과 같은 요청이다.
    assert put(client, story, {**payload, "presets": None}, "old-key").status_code == 200
    # 같은 키에 프리셋을 실으면 다른 요청이다.
    changed = put(client, story, {**payload, "presets": []}, "old-key")
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(canonical(dsn, story)) == 1
