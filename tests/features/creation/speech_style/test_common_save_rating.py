"""CREATE-US-08 1차 조각: 공통 저장의 등급 선택(AC01·AC13)과 제작자 목록 표시(AC12 일부).

- 요청 `requested_rating`은 선택이다. 빼면 기존 선택을 유지하고 기존 요청 해시와 같다.
- 비공개·공개 스토리의 임시 저장은 선택을 임시 저장본에만 두고, 「저장」이 반영한다.
- 미등록 스토리는 선택만 본 테이블에 바로 쓴다.
- 적용 등급·검토 상태·사유·요청 시각은 어떤 공통 저장으로도 바뀌지 않는다.
"""

import hashlib
import json

import psycopg
import pytest
from pydantic import ValidationError

from app.features.creation.save_visibility.schemas import CreationSaveRequest
from app.features.creation.save_visibility.service import request_digest, with_latest_scene
from tests.features.creation.speech_style.test_common_save_api import (
    body,
    current_payload,
    put,
    url,
)
from tests.features.creation.speech_style.test_working_copy_save import (
    publish,
    stored_custom_style,
    working_copy_rows,
)

LIST_URL = "/api/v1/creation/stories"

# (effective_rating, rating_review_status, rating_review_reason, rating_requested_at 유무)
REVIEW_STATES = [
    ("SAFE", "not_reviewed", None, False),
    ("UNSAFE", "reviewing", None, True),
    ("SAFE", "completed", "검토 완료 사유", True),
    ("UNSAFE", "failed", "검토 실패 사유", True),
]


def rating_row(dsn, story):
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """SELECT requested_rating, effective_rating, rating_review_status,
                      rating_review_reason, rating_requested_at
               FROM stories WHERE id=%s""",
            (story["id"],),
        ).fetchone()
    return {
        "requested_rating": row[0],
        "effective_rating": row[1],
        "rating_review_status": row[2],
        "rating_review_reason": row[3],
        "rating_requested_at": row[4],
    }


def set_review_state(dsn, story, state, requested="SAFE"):
    """검토 결과가 이미 있는 스토리를 흉내 낸다(이 조각은 검토를 쓰지 않으므로 직접 넣는다)."""
    effective, status, reason, has_time = state
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """UPDATE stories SET requested_rating=%s, effective_rating=%s,
                      rating_review_status=%s, rating_review_reason=%s,
                      rating_requested_at=CASE WHEN %s THEN clock_timestamp() END
               WHERE id=%s""",
            (requested, effective, status, reason, has_time, story["id"]),
        )
    return rating_row(dsn, story)


def review_part(row):
    """공통 저장이 바꾸면 안 되는 저장본 값."""
    return {key: value for key, value in row.items() if key != "requested_rating"}


def staged_rating(dsn, story):
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT creation->>'requested_rating' FROM story_working_copies WHERE story_id=%s",
            (story["id"],),
        ).fetchone()
    return row[0] if row else None


# --- 요청 계약(DB 불필요) -------------------------------------------------------------------


def test_requested_rating_is_optional_and_accepts_only_safe_or_unsafe_CREATE_US_08_AC01():
    base = body()
    assert CreationSaveRequest.model_validate(base).requested_rating is None
    for value in ("SAFE", "UNSAFE"):
        request = CreationSaveRequest.model_validate({**base, "requested_rating": value})
        assert request.requested_rating == value
    # 보낼 때는 null을 받지 않고, 대소문자·다른 값도 바꾸지 않고 거절한다.
    for value in (None, "safe", "Unsafe", "", "R19", 0, True, ["SAFE"]):
        with pytest.raises(ValidationError):
            CreationSaveRequest.model_validate({**base, "requested_rating": value})


@pytest.mark.parametrize(
    "extra",
    [
        {"effective_rating": "UNSAFE"},
        {"rating_review_status": "completed"},
        {"rating_review_reason": "사유"},
        {"rating_requested_at": "2026-11-22T00:00:00Z"},
    ],
)
def test_request_cannot_set_effective_rating_or_review_CREATE_US_08_AC01(extra):
    # 기존 계약처럼 모르는 필드는 거절한다(extra=forbid). 적용 등급·검토 상태를 올릴 수 없다.
    with pytest.raises(ValidationError):
        CreationSaveRequest.model_validate({**body(), **extra})


def test_finalize_autosave_rejects_explicit_rating_CREATE_US_08_AC13():
    base = {**body("private"), "preserve_scene": True, "finalize_auto_request_id": "auto-1"}
    assert CreationSaveRequest.model_validate(base)
    with pytest.raises(ValidationError):
        CreationSaveRequest.model_validate({**base, "requested_rating": "UNSAFE"})


def _legacy_digest(story_id, request):
    """등급 연결 전 request_digest와 같은 계산(장면·선택 필드 생략 규칙 포함)."""
    exclude = {
        "presets",
        "basic",
        "images",
        "finalize_auto_request_id",
        "requested_rating",
        "finalize_rating_working_copy_at",
    }
    exclude.update(
        ("fewshots", "expression", "intros", "relations", "affinities")
        if request.preserve_scene
        else ("preserve_scene",)
    )
    payload = {
        "operation": "creation-save-v4",
        "story_id": str(story_id),
        **request.model_dump(mode="json", exclude=exclude),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("preserve_scene", [False, True])
def test_omitted_rating_keeps_legacy_digest_CREATE_US_08_AC13(preserve_scene):
    base = {**body("private"), "kind": "temporary", "preserve_scene": preserve_scene}
    omitted = CreationSaveRequest.model_validate(base)
    assert request_digest(7, omitted) == _legacy_digest(7, omitted)
    safe = CreationSaveRequest.model_validate({**base, "requested_rating": "SAFE"})
    unsafe = CreationSaveRequest.model_validate({**base, "requested_rating": "UNSAFE"})
    # 등급을 보낸 요청은 생략한 요청·다른 등급 요청과 같은 요청키로 섞이지 않는다.
    digests = {request_digest(7, request) for request in (omitted, safe, unsafe)}
    assert len(digests) == 3


@pytest.mark.parametrize("rating", [None, "UNSAFE"])
def test_latest_scene_revalidation_keeps_rating_choice_CREATE_US_08_AC13(rating):
    # preserve_scene 요청은 최신 장면으로 다시 검증한다. 생략한 등급이 null 거절로 바뀌지 않는다.
    sent = {**body("private"), "kind": "temporary", "preserve_scene": True}
    if rating is not None:
        sent["requested_rating"] = rating
    staged = {**body("private", text="최신 장면"), "save_state": "public"}
    story = {"id": 1, "save_state": "private"}
    request = CreationSaveRequest.model_validate(sent)
    # 임시 저장본 장면이 있으면 DB를 읽지 않는다(connection=None).
    result = with_latest_scene(None, story, request, {"creation": staged})
    assert result.requested_rating == rating
    assert result.expression.custom_style == "최신 장면"


# --- 미등록(draft) 스토리 -------------------------------------------------------------------


@pytest.mark.parametrize("state", REVIEW_STATES)
def test_draft_save_stores_selection_only_and_reopens_CREATE_US_08_AC01(context, state):
    client, _, story, _, dsn = context
    before = set_review_state(dsn, story, state)
    result = put(client, story, {**body(), "requested_rating": "UNSAFE"}, "draft-rating")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["requested_rating"] == "UNSAFE"
    assert data["effective_rating"] == state[0]
    assert data["rating_review_status"] == state[1]
    after = rating_row(dsn, story)
    assert after["requested_rating"] == "UNSAFE"
    assert review_part(after) == review_part(before)
    reopened = client.get(url(story)).json()
    assert reopened["requested_rating"] == "UNSAFE"
    assert reopened["effective_rating"] == state[0]
    assert reopened["rating_review_status"] == state[1]


@pytest.mark.parametrize("preserve_scene", [False, True])
def test_draft_temporary_save_writes_selected_value_directly_CREATE_US_08_AC01(
    context, preserve_scene
):
    client, _, story, _, dsn = context
    before = set_review_state(dsn, story, REVIEW_STATES[2])
    payload = {
        **body(),
        "kind": "temporary",
        "preserve_scene": preserve_scene,
        "requested_rating": "UNSAFE",
    }
    result = put(client, story, payload, f"draft-temp-{preserve_scene}")
    assert result.status_code == 200, result.text
    assert result.json()["requested_rating"] == "UNSAFE"
    assert result.json()["working_copy_saved_at"] is None
    assert working_copy_rows(dsn, story) == 0
    after = rating_row(dsn, story)
    assert after["requested_rating"] == "UNSAFE"
    assert review_part(after) == review_part(before)


def test_draft_save_without_rating_keeps_selection_CREATE_US_08_AC13(context):
    client, _, story, _, dsn = context
    before = set_review_state(dsn, story, REVIEW_STATES[1], requested="UNSAFE")
    result = put(client, story, body(text="다른 영역만 수정"), "draft-omitted")
    assert result.status_code == 200, result.text
    assert result.json()["requested_rating"] == "UNSAFE"
    assert rating_row(dsn, story) == before


# --- 비공개·공개 스토리의 임시 저장과 저장 -----------------------------------------------------


@pytest.mark.parametrize("mode", ["temporary", "auto"])
def test_completed_story_temporary_rating_stays_in_working_copy_CREATE_US_08_AC13(context, mode):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    before = set_review_state(dsn, story, REVIEW_STATES[2])
    payload = {**current_payload(saved), "requested_rating": "UNSAFE"}
    payload.update({"kind": "temporary"} if mode == "temporary" else {"mode": "auto"})
    result = put(client, story, payload, f"staged-{mode}")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["working_copy_saved_at"] is not None
    assert data["requested_rating"] == "UNSAFE"
    assert data["effective_rating"] == "SAFE"
    assert data["rating_review_status"] == "completed"
    # 저장본은 그대로이고 임시 저장본에만 선택이 있다.
    assert rating_row(dsn, story) == before
    assert staged_rating(dsn, story) == "UNSAFE"
    reopened = client.get(url(story)).json()
    assert reopened["requested_rating"] == "UNSAFE"
    assert reopened["effective_rating"] == "SAFE"
    assert reopened["rating_review_status"] == "completed"


def test_other_section_temporary_save_preserves_pending_rating_CREATE_US_08_AC13(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    before = rating_row(dsn, story)
    staged = {**current_payload(saved), "kind": "temporary", "requested_rating": "UNSAFE"}
    assert put(client, story, staged, "rating").status_code == 200
    # 등급을 맡지 않는 장면 임시 저장(등급 생략)
    scene = current_payload(saved)
    scene["expression"]["custom_style"] = "장면만 고침"
    assert put(client, story, {**scene, "kind": "temporary"}, "scene").status_code == 200
    # 프로필 화면의 preserve_scene 임시 저장(등급 생략)
    profile = {
        **current_payload(saved),
        "kind": "temporary",
        "preserve_scene": True,
        "presets": [{"name": "", "description": "작성 중"}],
    }
    result = put(client, story, profile, "profile")
    assert result.status_code == 200, result.text
    assert result.json()["requested_rating"] == "UNSAFE"
    reopened = client.get(url(story)).json()
    assert reopened["requested_rating"] == "UNSAFE"
    assert reopened["expression"]["custom_style"] == "장면만 고침"
    assert staged_rating(dsn, story) == "UNSAFE"
    assert rating_row(dsn, story) == before


@pytest.mark.parametrize("state", REVIEW_STATES)
def test_final_save_promotes_pending_rating_only_CREATE_US_08_AC13(context, state):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    before = set_review_state(dsn, story, state)
    staged = {**current_payload(saved), "kind": "temporary", "requested_rating": "UNSAFE"}
    assert put(client, story, staged, "stage").status_code == 200
    # 「저장」은 등급을 빼도 임시 저장한 선택을 반영한다.
    result = put(client, story, current_payload(saved), "commit")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["working_copy_saved_at"] is None
    assert data["requested_rating"] == "UNSAFE"
    assert data["effective_rating"] == state[0]
    assert data["rating_review_status"] == state[1]
    after = rating_row(dsn, story)
    assert after["requested_rating"] == "UNSAFE"
    assert review_part(after) == review_part(before)
    assert working_copy_rows(dsn, story) == 0
    reopened = client.get(url(story)).json()
    assert reopened["requested_rating"] == "UNSAFE"
    assert reopened["working_copy_saved_at"] is None


def test_final_save_explicit_rating_wins_over_pending_CREATE_US_08_AC13(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    staged = {**current_payload(saved), "kind": "temporary", "requested_rating": "UNSAFE"}
    assert put(client, story, staged, "stage").status_code == 200
    result = put(client, story, {**current_payload(saved), "requested_rating": "SAFE"}, "commit")
    assert result.status_code == 200, result.text
    assert result.json()["requested_rating"] == "SAFE"
    assert rating_row(dsn, story)["requested_rating"] == "SAFE"


def test_final_save_without_pending_or_rating_keeps_canonical_CREATE_US_08_AC13(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    before = set_review_state(dsn, story, REVIEW_STATES[3], requested="UNSAFE")
    result = put(client, story, current_payload(saved), "commit")
    assert result.status_code == 200, result.text
    assert result.json()["requested_rating"] == "UNSAFE"
    assert rating_row(dsn, story) == before


@pytest.mark.parametrize("explicit", [None, "SAFE"])
def test_failed_final_save_rolls_back_rating_and_keeps_pending_CREATE_US_08_AC13(context, explicit):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    before = set_review_state(dsn, story, REVIEW_STATES[1], requested="SAFE")
    staged = {**current_payload(saved), "kind": "temporary", "requested_rating": "UNSAFE"}
    assert put(client, story, staged, "stage").status_code == 200
    # 인트로를 모두 지운 「저장」은 완성 검사에서 거절된다.
    failing = {**current_payload(saved), "intros": []}
    if explicit is not None:
        failing["requested_rating"] = explicit
    rejected = put(client, story, failing, "commit-fail")
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INTRO_REQUIRED"
    assert rating_row(dsn, story) == before
    assert staged_rating(dsn, story) == "UNSAFE"
    assert client.get(url(story)).json()["requested_rating"] == "UNSAFE"


# --- 재전송·잘못된 값 ----------------------------------------------------------------------


def test_same_key_replay_returns_recorded_rating_and_rejects_changed_rating_CREATE_US_08_AC13(
    context,
):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    payload = {**current_payload(saved), "kind": "temporary", "requested_rating": "UNSAFE"}
    first = put(client, story, payload, "replay")
    assert first.status_code == 200
    assert put(client, story, payload, "replay").json() == first.json()
    changed_rating = put(client, story, {**payload, "requested_rating": "SAFE"}, "replay")
    assert changed_rating.status_code == 409
    assert changed_rating.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    omitted = {key: value for key, value in payload.items() if key != "requested_rating"}
    assert put(client, story, omitted, "replay").status_code == 409
    assert staged_rating(dsn, story) == "UNSAFE"


def test_replay_of_response_recorded_before_rating_fields_still_works_CREATE_US_08_AC13(
    context,
):
    client, _, story, owner_id, dsn = context
    saved = publish(client, story, dsn)
    payload = {**current_payload(saved), "kind": "temporary"}
    assert put(client, story, payload, "legacy").status_code == 200
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """UPDATE creation_write_requests
               SET response = response - 'requested_rating' - 'effective_rating'
                              - 'rating_review_status'
               WHERE owner_id=%s AND request_id='legacy'""",
            (owner_id,),
        )
    replayed = put(client, story, payload, "legacy")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["requested_rating"] == "SAFE"
    assert replayed.json()["effective_rating"] == "SAFE"
    assert replayed.json()["rating_review_status"] == "not_reviewed"


@pytest.mark.parametrize(
    "extra",
    [
        {"requested_rating": None},
        {"requested_rating": "safe"},
        {"requested_rating": "R19"},
        {"effective_rating": "UNSAFE"},
        {"rating_review_status": "completed"},
    ],
)
def test_invalid_rating_request_writes_nothing_CREATE_US_08_AC01(context, extra):
    client, _, story, owner_id, dsn = context
    saved = publish(client, story, dsn)
    before = rating_row(dsn, story)
    rejected = put(client, story, {**current_payload(saved), **extra}, "invalid")
    assert rejected.status_code == 422
    assert rating_row(dsn, story) == before
    assert working_copy_rows(dsn, story) == 0
    with psycopg.connect(dsn) as connection:
        recorded = connection.execute(
            "SELECT count(*) FROM creation_write_requests WHERE owner_id=%s AND request_id=%s",
            (owner_id, "invalid"),
        ).fetchone()[0]
    assert recorded == 0


# --- 등급만 담은 임시 저장본 반영(finalize_rating_working_copy_at) ----------------------------


def rating_only_stage(saved, rating="UNSAFE"):
    return {
        **current_payload(saved),
        "kind": "temporary",
        "preserve_scene": True,
        "requested_rating": rating,
    }


def rating_finalize(saved, saved_at, **extra):
    return {
        **current_payload(saved),
        "preserve_scene": True,
        "finalize_rating_working_copy_at": saved_at,
        **extra,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "temporary"},
        {"mode": "auto"},
        {"preserve_scene": False},
        {"presets": []},
        {"basic": {"title": "제목"}},
        {"images": {"story_image_ids": []}},
        {"finalize_auto_request_id": "auto-1"},
    ],
)
def test_rating_finalize_request_contract_CREATE_US_08_AC13(extra):
    base = {
        **body("private"),
        "preserve_scene": True,
        "finalize_rating_working_copy_at": "2026-11-22T00:00:00.123456+00:00",
    }
    assert CreationSaveRequest.model_validate(base)
    assert CreationSaveRequest.model_validate({**base, "requested_rating": "SAFE"})
    with pytest.raises(ValidationError):
        CreationSaveRequest.model_validate({**base, **extra})


def test_omitted_rating_finalize_keeps_legacy_digest_CREATE_US_08_AC13():
    base = {**body("private"), "preserve_scene": True}
    omitted = CreationSaveRequest.model_validate(base)
    assert request_digest(7, omitted) == _legacy_digest(7, omitted)
    sent = CreationSaveRequest.model_validate(
        {**base, "finalize_rating_working_copy_at": "2026-11-22T00:00:00+00:00"}
    )
    assert request_digest(7, sent) != request_digest(7, omitted)


@pytest.mark.parametrize("explicit", [None, "SAFE"])
def test_rating_only_working_copy_finalizes_rating_CREATE_US_08_AC13(context, explicit):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    before = set_review_state(dsn, story, REVIEW_STATES[2])
    staged = put(client, story, rating_only_stage(saved), "rating-stage")
    assert staged.status_code == 200, staged.text
    assert staged.json()["rating_only_working_copy"] is True
    reopened = client.get(url(story)).json()
    assert reopened["rating_only_working_copy"] is True
    extra = {} if explicit is None else {"requested_rating": explicit}
    finalize = rating_finalize(saved, staged.json()["working_copy_saved_at"], **extra)
    result = put(client, story, finalize, "rating-finalize")
    assert result.status_code == 200, result.text
    data = result.json()
    expected = explicit or "UNSAFE"
    assert data["requested_rating"] == expected
    assert data["rating_only_working_copy"] is False
    assert data["working_copy_saved_at"] is None
    assert data["save_state"] == "private"
    after = rating_row(dsn, story)
    assert after["requested_rating"] == expected
    assert review_part(after) == review_part(before)
    assert working_copy_rows(dsn, story) == 0


def _stale_by_restage(client, story, saved, dsn):
    first = put(client, story, rating_only_stage(saved), "s1").json()
    assert put(client, story, rating_only_stage(saved, "SAFE"), "s2").status_code == 200
    return first["working_copy_saved_at"], {}


def _mixed_by_profile(client, story, saved, dsn):
    first = put(client, story, rating_only_stage(saved), "s1").json()
    profile = {
        **current_payload(saved),
        "kind": "temporary",
        "preserve_scene": True,
        "presets": [{"name": "", "description": "작성 중"}],
    }
    mixed = put(client, story, profile, "s2").json()
    assert mixed["rating_only_working_copy"] is False
    return first["working_copy_saved_at"], {}


def _mixed_then_rating(client, story, saved, dsn):
    # US07 혼합 임시 저장본 위에 등급을 써도 등급 전용으로 보지 않는다.
    scene = current_payload(saved)
    scene["expression"]["custom_style"] = "장면 수정 중"
    assert put(client, story, {**scene, "kind": "temporary"}, "s1").status_code == 200
    rated = put(client, story, rating_only_stage(saved), "s2").json()
    assert rated["rating_only_working_copy"] is False
    return rated["working_copy_saved_at"], {}


def _image_working_copy(client, story, saved, dsn):
    staged = put(client, story, rating_only_stage(saved), "s1").json()
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """UPDATE story_working_copies
               SET images='{"story_image_ids": [], "characters": []}'::jsonb,
                   images_saved_at=clock_timestamp()
               WHERE story_id=%s""",
            (story["id"],),
        )
    assert client.get(url(story)).json()["rating_only_working_copy"] is False
    return staged["working_copy_saved_at"], {}


def _other_visibility(client, story, saved, dsn):
    # 비공개→공개만 허용한다. 공개 스토리를 비공개로 되돌리는 반영은 그대로 거절한다.
    public = put(client, story, current_payload(saved, "public"), "go-public")
    assert public.status_code == 200, public.text
    staged = put(client, story, rating_only_stage(public.json()), "s1").json()
    return staged["working_copy_saved_at"], {"save_state": "private"}


def _stale_by_restage_to_public(client, story, saved, dsn):
    first = put(client, story, rating_only_stage(saved), "s1").json()
    assert put(client, story, rating_only_stage(saved, "SAFE"), "s2").status_code == 200
    return first["working_copy_saved_at"], {"save_state": "public"}


@pytest.mark.parametrize(
    "arrange",
    [
        _stale_by_restage,
        _mixed_by_profile,
        _mixed_then_rating,
        _image_working_copy,
        _other_visibility,
        _stale_by_restage_to_public,
    ],
)
def test_rating_finalize_rejects_stale_or_mixed_copy_CREATE_US_08_AC13(context, arrange):
    client, _, story, owner_id, dsn = context
    saved = publish(client, story, dsn)
    before = rating_row(dsn, story)
    saved_at, extra = arrange(client, story, saved, dsn)
    with psycopg.connect(dsn) as connection:
        copy_before = connection.execute(
            "SELECT creation, creation_saved_at FROM story_working_copies WHERE story_id=%s",
            (story["id"],),
        ).fetchone()
    rejected = put(client, story, rating_finalize(saved, saved_at, **extra), "finalize")
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "CREATION_RATING_WORKING_COPY_CHANGED"
    # 아무것도 쓰지 않는다: 저장본·임시 저장본·요청키 기록이 그대로다.
    assert rating_row(dsn, story) == before
    with psycopg.connect(dsn) as connection:
        copy_after = connection.execute(
            "SELECT creation, creation_saved_at FROM story_working_copies WHERE story_id=%s",
            (story["id"],),
        ).fetchone()
        recorded = connection.execute(
            "SELECT count(*) FROM creation_write_requests WHERE owner_id=%s AND request_id=%s",
            (owner_id, "finalize"),
        ).fetchone()[0]
    assert copy_after == copy_before
    assert recorded == 0


def commit_newer_scene(dsn, story, text="정본 최신 장면"):
    """임시 저장 뒤 다른 경로가 정본 장면을 바꾼 상황을 흉내 낸다."""
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE story_expression_settings SET custom_style=%s WHERE story_id=%s",
            (text, story["id"]),
        )


def test_rating_finalize_private_to_public_uses_latest_committed_scene_CREATE_US_08_AC13(
    context,
):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    before = set_review_state(dsn, story, REVIEW_STATES[2])
    staged = put(client, story, rating_only_stage(saved), "rating-stage")
    assert staged.status_code == 200, staged.text
    assert staged.json()["rating_only_working_copy"] is True
    commit_newer_scene(dsn, story)
    finalize = rating_finalize(saved, staged.json()["working_copy_saved_at"], save_state="public")
    result = put(client, story, finalize, "rating-finalize-public")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["save_state"] == "public"
    assert data["requested_rating"] == "UNSAFE"
    assert data["working_copy_saved_at"] is None
    assert data["rating_only_working_copy"] is False
    assert data["expression"]["custom_style"] == "정본 최신 장면"
    assert stored_custom_style(dsn, story) == "정본 최신 장면"
    after = rating_row(dsn, story)
    assert after["requested_rating"] == "UNSAFE"
    assert review_part(after) == review_part(before)
    assert working_copy_rows(dsn, story) == 0


@pytest.mark.parametrize("restage", [False, True], ids=["direct", "omitted-rating-restage"])
@pytest.mark.parametrize("state", ["private", "public"])
def test_tokenless_final_save_on_rating_only_copy_keeps_latest_scene_CREATE_US_08_AC13(
    context, state, restage
):
    # 예전 클라이언트: 등급 전용 임시 저장본을 토큰 없이 preserve_scene 「저장」으로 반영한다.
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    staged = put(client, story, rating_only_stage(saved), "rating-stage")
    assert staged.status_code == 200, staged.text
    assert staged.json()["rating_only_working_copy"] is True
    if restage:
        # 등급을 빼고 다시 보낸 preserve_scene 임시 저장도 등급 전용 표시를 잃지 않는다.
        again = {k: v for k, v in rating_only_stage(saved).items() if k != "requested_rating"}
        restaged = put(client, story, again, "rating-restage")
        assert restaged.status_code == 200, restaged.text
        assert restaged.json()["rating_only_working_copy"] is True
        assert restaged.json()["requested_rating"] == "UNSAFE"
    commit_newer_scene(dsn, story)
    final = {**current_payload(saved, state), "preserve_scene": True}
    result = put(client, story, final, "legacy-final")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["save_state"] == state
    assert data["requested_rating"] == "UNSAFE"
    assert data["working_copy_saved_at"] is None
    assert stored_custom_style(dsn, story) == "정본 최신 장면"
    assert rating_row(dsn, story)["requested_rating"] == "UNSAFE"
    assert working_copy_rows(dsn, story) == 0


def test_tokenless_final_save_keeps_genuine_scene_copy_CREATE_US_08_AC13(context):
    # 장면을 담은 임시 저장본은 예전처럼 그 장면을 반영한다.
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    scene = current_payload(saved)
    scene["expression"]["custom_style"] = "임시 장면"
    assert put(client, story, {**scene, "kind": "temporary"}, "s1").status_code == 200
    rated = put(client, story, rating_only_stage(saved), "s2").json()
    assert rated["rating_only_working_copy"] is False
    commit_newer_scene(dsn, story)
    final = {**current_payload(saved), "preserve_scene": True}
    result = put(client, story, final, "legacy-final")
    assert result.status_code == 200, result.text
    assert stored_custom_style(dsn, story) == "임시 장면"
    assert result.json()["requested_rating"] == "UNSAFE"


# --- 제작자 목록(AC12 일부) ---------------------------------------------------------------


def test_creator_list_shows_canonical_rating_and_review_status_CREATE_US_08_AC12(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    set_review_state(dsn, story, REVIEW_STATES[3])
    staged = {**current_payload(saved), "kind": "temporary", "requested_rating": "SAFE"}
    assert put(client, story, staged, "stage").status_code == 200
    listed = client.get(LIST_URL)
    assert listed.status_code == 200
    item = next(entry for entry in listed.json() if entry["id"] == story["id"])
    assert item["effective_rating"] == "UNSAFE"
    assert item["rating_review_status"] == "failed"
    # 목록은 저장본 값만 싣는다. 선택·검토 사유는 목록에 넣지 않는다.
    assert "requested_rating" not in item
    assert "rating_review_reason" not in item


def test_creator_list_defaults_for_new_story_CREATE_US_08_AC12(context):
    client, _, story, _, _ = context
    item = next(entry for entry in client.get(LIST_URL).json() if entry["id"] == story["id"])
    assert item["effective_rating"] == "SAFE"
    assert item["rating_review_status"] == "not_reviewed"
