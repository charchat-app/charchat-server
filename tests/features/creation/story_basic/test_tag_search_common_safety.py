"""CREATE-US-08-AC15 공개 해시태그 검색의 공통 안전 판정(주입 시에만)."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.common_safety import CommonSafety
from app.main import create_app
from tests.common_safety_fakes import BLOCK_MARKER, FakeGuard

TAGS = "/api/v1/creation/tags/stories"
OWNER = {"X-User-Id": "1"}


def _public_story(db, title: str, world: str = "World", rating: str = "SAFE") -> int:
    story = db.execute(
        """INSERT INTO stories (owner_id, title, subtitle, world_description, save_state,
                               requested_rating, effective_rating)
           VALUES (1, %s, 'Sub', %s, 'public', %s, %s) RETURNING id""",
        (title, world, rating, rating),
    ).fetchone()["id"]
    tag = db.execute(
        """INSERT INTO tags (name) VALUES ('romance')
           ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"""
    ).fetchone()["id"]
    db.execute("INSERT INTO story_tags (story_id, tag_id) VALUES (%s, %s)", (story, tag))
    return story


def _search(db_url, guard):
    with TestClient(create_app(db_url, common_safety=guard)) as client:
        return client.get(TAGS, params={"tag": "romance"}, headers=OWNER)


def test_blocked_story_is_omitted_from_tag_search_CREATE_US_08_AC15(db, db_url):
    clear = _public_story(db, "Clear", rating="UNSAFE")
    _public_story(db, "Blocked", world=BLOCK_MARKER)
    guard = FakeGuard()
    response = _search(db_url, guard)
    assert response.status_code == 200
    assert [s["id"] for s in response.json()] == [clear]
    assert BLOCK_MARKER not in response.text
    # 노출되지 않는 세계관까지 정본으로 판정했다.
    assert {s.world_description for s in guard.surfaces} == {"World", BLOCK_MARKER}


@pytest.mark.parametrize("outcome", [CommonSafety.UNAVAILABLE, RuntimeError("down"), None])
def test_unavailable_evaluator_hides_all_results_CREATE_US_08_AC15(db, db_url, outcome):
    _public_story(db, "One")
    response = _search(db_url, FakeGuard(outcome))
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("rating", ["SAFE", "UNSAFE"])
def test_neutral_clear_keeps_results_for_both_ratings_CREATE_US_08_AC15(db, db_url, rating):
    story = _public_story(db, "One", rating=rating)
    assert [s["id"] for s in _search(db_url, FakeGuard()).json()] == [story]


def test_search_result_matches_evaluated_snapshot_CREATE_US_08_AC15(db, db_url):
    story = _public_story(db, "Before")

    def edit(_surface):
        with psycopg.connect(db_url, autocommit=True) as other:
            other.execute("UPDATE stories SET title = 'After' WHERE id = %s", (story,))

    body = _search(db_url, FakeGuard(on_call=edit)).json()
    assert [s["title"] for s in body] == ["Before"]


def test_default_app_tag_search_is_unchanged_CREATE_US_08_AC15(client, db):
    story = _public_story(db, "Blocked", world=BLOCK_MARKER)
    assert [s["id"] for s in client.get(TAGS, params={"tag": "romance"}, headers=OWNER).json()] == [
        story
    ]
