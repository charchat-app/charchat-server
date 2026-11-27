"""CREATE-US-09-AC04·AC07: 확정본만 분류 대상으로 표시한다."""

import pytest

from tests.features.creation.speech_style.test_common_save_api import body, complete, put

STORIES = "/api/v1/creation/stories"
pytestmark = pytest.mark.usefixtures("dev_user_header_enabled")


def create_complete(client, db_url):
    response = client.post(
        STORIES, json={"title": "검수 스토리", "world_description": "기존 세계관"}
    )
    assert response.status_code == 201
    story = response.json()
    complete(db_url, story)
    return story


def pending(db, story):
    return db.execute(
        "SELECT category_classification_needed FROM stories WHERE id=%s", (story["id"],)
    ).fetchone()["category_classification_needed"]


@pytest.mark.parametrize("target", ["private", "public"])
def test_registration_marks_pending_CREATE_US_09_AC04(client, db, db_url, target):
    client.headers["X-User-Id"] = "100"
    story = create_complete(client, db_url)
    temp = {**body(target), "kind": "temporary"}
    assert put(client, story, temp, "stage").status_code == 200
    assert not pending(db, story)
    result = put(client, story, body(target), "register")
    assert result.status_code == 200
    assert pending(db, story)
    assert "category_classification_needed" not in result.json()
    db.execute(
        "UPDATE stories SET category_classification_needed=false WHERE id=%s", (story["id"],)
    )
    assert put(client, story, body(target), "register").status_code == 200
    assert not pending(db, story)


def test_only_committed_category_inputs_mark_pending_CREATE_US_09_AC07(client, db, db_url):
    client.headers["X-User-Id"] = "100"
    story = create_complete(client, db_url)
    assert put(client, story, body("private"), "register").status_code == 200
    db.execute(
        "UPDATE stories SET category_classification_needed=false WHERE id=%s", (story["id"],)
    )
    endpoint = f"{STORIES}/{story['id']}"
    basic = {"title": "변경 제목", "subtitle": "한 줄 소개", "world_description": "기존 세계관"}
    assert client.put(endpoint, json=basic).status_code == 200
    assert not pending(db, story)
    staged = {
        **body("private"),
        "kind": "temporary",
        "basic": {**basic, "world_description": "세계관"},
    }
    assert put(client, story, staged, "stage").status_code == 200
    assert not pending(db, story)
    assert put(client, story, body("private"), "apply").status_code == 200
    assert pending(db, story)
    timestamp_sql = "SELECT saved_at FROM stories WHERE id=%s"
    saved_at = db.execute(timestamp_sql, (story["id"],)).fetchone()["saved_at"]
    db.execute(
        "UPDATE stories SET category_classification_needed=false WHERE id=%s", (story["id"],)
    )
    assert db.execute(timestamp_sql, (story["id"],)).fetchone()["saved_at"] == saved_at
    direct = {**basic, "world_description": "확정 세계관"}
    assert client.put(endpoint, json=direct).status_code == 200
    assert pending(db, story)
    db.execute(
        "UPDATE stories SET category_classification_needed=false WHERE id=%s", (story["id"],)
    )
    cid = db.execute("SELECT id FROM characters WHERE story_id=%s", (story["id"],)).fetchone()["id"]
    rename = {**direct, "characters": [{"id": cid, "name": "윤서", "description": "원래 상세"}]}
    assert client.put(endpoint, json=rename).status_code == 200
    assert not pending(db, story)
    rename["characters"][0]["description"] = "바뀐 인물 상세"
    assert client.put(endpoint, json=rename).status_code == 200
    assert pending(db, story)
