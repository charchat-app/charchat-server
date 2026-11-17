"""CREATE-US-01 저장 피처: AC01·AC02·AC06."""

import psycopg
import pytest
from fastapi.testclient import TestClient

URL = "/api/v1/creation/stories"
OWNER = {"X-User-Id": "1"}
OTHER = {"X-User-Id": "2"}

FULL = {
    "title": "달빛 서점",
    "subtitle": "밤에만 문을 여는 서점의 비밀",
    "world_description": "도시 골목 끝, 자정에만 열리는 서점이 있다.\n주인은 말이 없다.",
    "tags": ["로맨스", "판타지"],
    # 캐릭터 조건(1명 이상)을 충족해 기본 정보 검사만 보이게 한다. 캐릭터 검사는 test_characters.py
    "characters": [{"name": "서윤", "description": "서점 주인. 말수가 적다."}],
}


def _create(client: TestClient, body: dict | None = None) -> dict:
    res = client.post(URL, json=FULL if body is None else body, headers=OWNER)
    assert res.status_code == 201, res.text
    return res.json()


def _issue_fields(story: dict) -> set[str]:
    return {i["field"] for i in story["issues"]}


# ---- AC01: 저장·복원, 제목과 한 줄 소개 구분 ----


def test_create_and_reload_basic_info_CREATE_US_01_AC01(client: TestClient) -> None:
    created = _create(client)
    assert created["save_state"] == "draft"

    res = client.get(f"{URL}/{created['id']}", headers=OWNER)
    assert res.status_code == 200
    story = res.json()
    assert story["title"] == FULL["title"]
    assert story["subtitle"] == FULL["subtitle"]
    assert story["world_description"] == FULL["world_description"]
    assert sorted(story["tags"]) == sorted(FULL["tags"])
    assert story["issues"] == []


def test_update_replaces_basic_info_and_tags_CREATE_US_01_AC01(client: TestClient) -> None:
    sid = _create(client)["id"]
    body = {
        "title": "새 제목",
        "subtitle": "새 소개",
        "world_description": "새 세계관",
        "tags": ["일상"],
    }
    res = client.put(f"{URL}/{sid}", json=body, headers=OWNER)
    assert res.status_code == 200, res.text

    story = client.get(f"{URL}/{sid}", headers=OWNER).json()
    assert (story["title"], story["subtitle"], story["world_description"]) == (
        "새 제목",
        "새 소개",
        "새 세계관",
    )
    assert story["tags"] == ["일상"]


def test_update_without_tags_keeps_existing_tags_CREATE_US_01_AC01(client: TestClient) -> None:
    sid = _create(client)["id"]
    body = {k: v for k, v in FULL.items() if k != "tags"}
    client.put(f"{URL}/{sid}", json=body, headers=OWNER)
    assert sorted(client.get(f"{URL}/{sid}", headers=OWNER).json()["tags"]) == sorted(FULL["tags"])


def test_tags_keep_original_spelling_CREATE_US_01_AC01(client: TestClient) -> None:
    story = _create(client, {**FULL, "tags": ["Romance", "romance", "Romance"]})
    assert sorted(story["tags"]) == ["Romance", "romance"]


def test_saved_at_changes_on_update_CREATE_US_01_AC01(client: TestClient) -> None:
    created = _create(client)
    updated = client.put(f"{URL}/{created['id']}", json=FULL, headers=OWNER).json()
    assert updated["saved_at"] > created["saved_at"]


# ---- AC02: 필수값 누락 안내, 입력 보존 ----


@pytest.mark.parametrize("missing", ["title", "subtitle", "world_description"])
def test_missing_required_field_is_reported_and_input_kept_CREATE_US_01_AC02(
    client: TestClient, missing: str
) -> None:
    body = {**FULL, missing: None}
    story = _create(client, body)
    assert _issue_fields(story) == {missing}
    assert story["issues"][0]["code"] == "REQUIRED"

    reloaded = client.get(f"{URL}/{story['id']}", headers=OWNER).json()
    for key in ("title", "subtitle", "world_description"):
        assert reloaded[key] == body[key]


def test_whitespace_only_is_missing_and_kept_as_is_CREATE_US_01_AC02(client: TestClient) -> None:
    story = _create(client, {**FULL, "title": "   "})
    assert _issue_fields(story) == {"title"}
    assert story["title"] == "   "


def test_empty_draft_is_saved_with_all_issues_CREATE_US_01_AC02(client: TestClient) -> None:
    story = _create(client, {})
    assert _issue_fields(story) == {"title", "subtitle", "world_description", "characters"}


def test_completed_story_rejects_missing_required_and_keeps_saved_CREATE_US_01_AC02(
    client: TestClient, db: psycopg.Connection
) -> None:
    sid = _create(client)["id"]
    db.execute("UPDATE stories SET save_state = 'private' WHERE id = %s", (sid,))

    res = client.put(f"{URL}/{sid}", json={**FULL, "title": ""}, headers=OWNER)
    assert res.status_code == 422
    assert res.headers["content-type"].startswith("application/problem+json")
    assert res.json()["code"] == "STORY_INCOMPLETE"
    assert [i["field"] for i in res.json()["issues"]] == ["title"]
    assert client.get(f"{URL}/{sid}", headers=OWNER).json()["title"] == FULL["title"]


# ---- AC06: 해시태그 없이도 저장 ----


@pytest.mark.parametrize("tags", [[], None])
def test_story_without_tags_has_no_issue_CREATE_US_01_AC06(
    client: TestClient, tags: list | None
) -> None:
    body = {k: v for k, v in FULL.items() if k != "tags"}
    if tags is not None:
        body["tags"] = tags
    story = _create(client, body)
    assert story["tags"] == []
    assert story["issues"] == []


# ---- 소유권·인증·에러 형식 ----


def test_other_user_cannot_read_or_update(client: TestClient) -> None:
    sid = _create(client)["id"]
    assert client.get(f"{URL}/{sid}", headers=OTHER).status_code == 404
    assert client.put(f"{URL}/{sid}", json=FULL, headers=OTHER).status_code == 404


def test_missing_story_is_404(client: TestClient) -> None:
    res = client.get(f"{URL}/999", headers=OWNER)
    assert res.status_code == 404
    assert res.json()["code"] == "STORY_NOT_FOUND"


def test_deleted_story_is_not_found(client: TestClient, db: psycopg.Connection) -> None:
    sid = _create(client)["id"]
    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (sid,))
    assert client.get(f"{URL}/{sid}", headers=OWNER).status_code == 404


@pytest.mark.parametrize("header", [{}, {"X-User-Id": "abc"}, {"X-User-Id": "0"}])
def test_missing_or_invalid_user_header_is_401(client: TestClient, header: dict) -> None:
    res = client.post(URL, json=FULL, headers=header)
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHENTICATED"


def test_invalid_body_returns_problem_details(client: TestClient) -> None:
    res = client.post(URL, json={"tags": "로맨스"}, headers=OWNER)
    assert res.status_code == 422
    assert res.headers["content-type"].startswith("application/problem+json")
    assert res.json()["code"] == "VALIDATION_ERROR"


# ---- 내 스토리 목록(다시 열기 진입점) ----


def test_list_shows_only_own_undeleted_stories_newest_first(
    client: TestClient, db: psycopg.Connection
) -> None:
    first = _create(client)["id"]
    second = _create(client, {**FULL, "title": "두 번째"})["id"]
    deleted = _create(client)["id"]
    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (deleted,))
    client.post(URL, json=FULL, headers=OTHER)

    res = client.get(URL, headers=OWNER)
    assert res.status_code == 200
    assert [s["id"] for s in res.json()] == [second, first]
    assert res.json()[0]["title"] == "두 번째"
    assert res.json()[0]["save_state"] == "draft"
