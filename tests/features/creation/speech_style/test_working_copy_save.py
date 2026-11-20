"""임시 저장본(2026-11-20 결정)의 2~4번 공통 저장 통합검사.

공개 상태(미등록·비공개·공개)와 저장 상태(임시 저장·저장)를 나눈다.
- 비공개·공개 스토리의 임시 저장은 임시 저장본에만 두고 이용자가 보는 저장본은 그대로다.
- 저장하면 반영하고 임시 저장본을 비운다.
- 미등록 스토리의 임시 저장은 본 테이블에 바로 쓴다.
"""

import psycopg

from tests.features.creation.speech_style.test_common_save_api import (
    body,
    complete,
    current_payload,
    put,
    url,
)


def stored_custom_style(dsn, story):
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT custom_style FROM story_expression_settings WHERE story_id=%s",
            (story["id"],),
        ).fetchone()
    return row[0] if row else None


def working_copy_rows(dsn, story):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT count(*) FROM story_working_copies WHERE story_id=%s", (story["id"],)
        ).fetchone()[0]


def publish(client, story, dsn, text="공개된 문체"):
    complete(dsn, story)
    result = put(client, story, body("private", text=text), "publish")
    assert result.status_code == 200, result.text
    return result.json()


def test_completed_story_temporary_save_keeps_public_content(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    payload = current_payload(saved)
    payload["expression"]["custom_style"] = "고치는 중인 문체"

    temporary = put(client, story, {**payload, "kind": "temporary"}, "temporary")
    assert temporary.status_code == 200
    data = temporary.json()
    assert data["working_copy_saved_at"] is not None
    assert data["expression"]["custom_style"] == "고치는 중인 문체"
    assert data["save_state"] == "private"
    # 이용자가 보는 저장본은 그대로다.
    assert stored_custom_style(dsn, story) == "공개된 문체"
    # 다시 열면 임시 저장본이 나온다.
    reopened = client.get(url(story)).json()
    assert reopened["expression"]["custom_style"] == "고치는 중인 문체"
    assert reopened["working_copy_saved_at"] is not None

    committed = put(client, story, payload, "commit")
    assert committed.status_code == 200
    assert committed.json()["working_copy_saved_at"] is None
    assert stored_custom_style(dsn, story) == "고치는 중인 문체"
    assert client.get(url(story)).json()["working_copy_saved_at"] is None
    assert working_copy_rows(dsn, story) == 0


def test_temporary_save_keeps_incomplete_input_without_checks(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    # 인트로를 모두 지운 상태는 완성 조건을 어기지만 임시 저장은 된다.
    incomplete = {**current_payload(saved), "intros": [], "kind": "temporary"}
    assert put(client, story, incomplete, "incomplete").status_code == 200
    assert client.get(url(story)).json()["intros"] == []
    # 저장(반영)할 때는 검사한다. 거절되면 임시 저장본은 그대로 남는다.
    rejected = put(client, story, {**incomplete, "kind": "save"}, "commit-incomplete")
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INTRO_REQUIRED"
    assert working_copy_rows(dsn, story) == 1
    with psycopg.connect(dsn) as connection:
        intros = connection.execute(
            "SELECT count(*) FROM intros WHERE story_id=%s", (story["id"],)
        ).fetchone()[0]
    assert intros == 1


def test_temporary_save_does_not_change_visibility(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    wants_public = {**current_payload(saved, "public"), "kind": "temporary"}
    result = put(client, story, wants_public, "temporary-public")
    assert result.status_code == 200
    assert result.json()["save_state"] == "private"
    assert client.get(url(story)).json()["save_state"] == "private"


def test_unregistered_story_temporary_save_writes_directly(context):
    client, _, story, _, dsn = context
    result = put(client, story, {**body(text="미등록 문체"), "kind": "temporary"}, "draft-temp")
    assert result.status_code == 200
    assert result.json()["working_copy_saved_at"] is None
    assert stored_custom_style(dsn, story) == "미등록 문체"
    assert working_copy_rows(dsn, story) == 0


def test_save_still_cannot_move_back_to_unregistered_or_private(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    payload = current_payload(saved)
    back = put(client, story, {**payload, "save_state": "draft"}, "back")
    assert back.status_code == 409
    assert back.json()["code"] == "SAVE_STATE_TRANSITION"
    assert put(client, story, {**payload, "save_state": "public"}, "public").status_code == 200
    assert put(client, story, payload, "back-private").status_code == 409
