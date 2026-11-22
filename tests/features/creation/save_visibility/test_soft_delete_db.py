"""CREATE-US-07-AC10 스토리 소프트 삭제 실제 PostgreSQL API 통합검사.

공용 픽스처(`tests/conftest.py`의 `client`·`db`)를 쓴다. `TEST_DATABASE_URL`이 없으면 건너뛴다.
공개 스토리는 기존 테스트 도우미(`create_story`)로 준비한다. 도우미는 draft로 넣은 뒤
DB 트리거가 허용하는 draft→public 전환으로 공개 상태를 만든다.
"""

import psycopg
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from tests.features.creation.images.helpers import OTHER_ID, OWNER_ID, create_story

STORIES_URL = "/api/v1/creation/stories"
OWNER = {"X-User-Id": str(OWNER_ID)}
OTHER = {"X-User-Id": str(OTHER_ID)}
WORKING_COPY = {"title": "고치던 제목"}


def _assert_not_found(response) -> None:
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "STORY_NOT_FOUND"


def _story_row(db: psycopg.Connection, story_id: int) -> dict:
    return db.execute(
        "SELECT owner_id, save_state, deleted_at FROM stories WHERE id = %s", (story_id,)
    ).fetchone()


def test_delete_public_story_keeps_row_and_children_CREATE_US_07_AC10(
    client: TestClient, db: psycopg.Connection
) -> None:
    """CREATE-US-07-AC10: 타인 404 → 본인 204 → 조회 404, DB에는 원본과 연결이 남는다."""
    story_id, (character_id,) = create_story(db, characters=("하린",), save_state="public")
    db.execute(
        """INSERT INTO story_working_copies(story_id, creation, creation_saved_at)
           VALUES (%s, %s, clock_timestamp())""",
        (story_id, Jsonb(WORKING_COPY)),
    )

    # 타인은 존재 여부를 알 수 없는 404를 받고, 스토리는 그대로다.
    _assert_not_found(client.delete(f"{STORIES_URL}/{story_id}", headers=OTHER))
    assert _story_row(db, story_id)["deleted_at"] is None

    response = client.delete(f"{STORIES_URL}/{story_id}", headers=OWNER)
    assert response.status_code == 204, response.text
    assert response.content == b""

    # 삭제 뒤 새 요청으로 조회하면 본인에게도 404다.
    _assert_not_found(client.get(f"{STORIES_URL}/{story_id}", headers=OWNER))
    _assert_not_found(client.get(f"{STORIES_URL}/{story_id}/creation", headers=OWNER))

    # 물리 삭제가 아니라 deleted_at만 기록하고, 공개 범위·소유자는 그대로 둔다.
    story = _story_row(db, story_id)
    assert story["deleted_at"] is not None
    assert story["save_state"] == "public"
    assert story["owner_id"] == OWNER_ID

    # 캐릭터와 임시 저장본 연결도 원본 스토리에 그대로 남는다.
    character = db.execute(
        "SELECT story_id, name FROM characters WHERE id = %s", (character_id,)
    ).fetchone()
    assert character == {"story_id": story_id, "name": "하린"}
    working_copy = db.execute(
        "SELECT creation FROM story_working_copies WHERE story_id = %s", (story_id,)
    ).fetchone()
    assert working_copy == {"creation": WORKING_COPY}
