"""CREATE-US-01 제한·순서 피처: AC09(합산 3000자)·AC14(캐릭터 순서 저장)."""

import psycopg
import pytest
from fastapi.testclient import TestClient

URL = "/api/v1/creation/stories"
OWNER = {"X-User-Id": "1"}


def _body(title: str, world: str, characters: list[tuple[str, str]], **extra) -> dict:
    return {
        "title": title,
        "subtitle": "소개",
        "world_description": world,
        "characters": [{"name": n, "description": d} for n, d in characters],
        **extra,
    }


def _create(client: TestClient, body: dict) -> dict:
    res = client.post(URL, json=body, headers=OWNER)
    assert res.status_code == 201, res.text
    return res.json()


def _codes(story: dict) -> set[str]:
    return {i["code"] for i in story["issues"]}


# 제목 2 + 소개 2 + 이름 2 = 6자, 세계관으로 나머지를 채운다
FIXED = 6


# ---- AC09: 합산 3000자 ----


@pytest.mark.parametrize(
    ("world_len", "over"), [(3000 - FIXED - 10, False), (3000 - FIXED - 9, True)]
)
def test_total_limit_boundary_CREATE_US_01_AC09(
    client: TestClient, world_len: int, over: bool
) -> None:
    # 캐릭터 상세 10자 포함: 정확히 3000자는 통과, 3001자는 초과
    story = _create(client, _body("제목", "가" * world_len, [("서윤", "상세상세상세상세상세")]))
    assert ("BASIC_TEXT_TOO_LONG" in _codes(story)) is over


def test_spaces_and_newlines_count_and_input_is_kept_CREATE_US_01_AC09(client: TestClient) -> None:
    world = "가 \n" * 1000  # 3000자 (공백·줄바꿈 포함)
    story = _create(client, _body("제목", world, [("서윤", "상세")]))
    issue = next(i for i in story["issues"] if i["code"] == "BASIC_TEXT_TOO_LONG")
    assert issue["field"] == "basic_text"
    assert issue["value"] == str(3000 + 2 + 2 + 2 + 2)
    # 임시 저장은 자르지 않고 그대로 보존
    assert client.get(f"{URL}/{story['id']}", headers=OWNER).json()["world_description"] == world


def test_tags_are_excluded_from_total_CREATE_US_01_AC09(client: TestClient) -> None:
    body = _body("제목", "가" * (3000 - FIXED - 10), [("서윤", "상세상세상세상세상세")])
    story = _create(client, {**body, "tags": ["로맨스", "판타지", "가나다라마바사"]})
    assert "BASIC_TEXT_TOO_LONG" not in _codes(story)


def test_adding_character_counts_toward_same_total_CREATE_US_01_AC09(client: TestClient) -> None:
    """인물이 늘어나도 총한도는 늘지 않는다."""
    story = _create(client, _body("제목", "가" * 2980, [("서윤", "상세")]))
    assert "BASIC_TEXT_TOO_LONG" not in _codes(story)
    res = client.put(
        f"{URL}/{story['id']}",
        json=_body("제목", "가" * 2980, [("서윤", "상세"), ("하린", "상세" * 10)]),
        headers=OWNER,
    )
    assert "BASIC_TEXT_TOO_LONG" in _codes(res.json())


def test_completed_story_rejects_over_limit_CREATE_US_01_AC09(
    client: TestClient, db: psycopg.Connection
) -> None:
    story = _create(client, _body("제목", "세계관", [("서윤", "상세")]))
    db.execute("UPDATE stories SET save_state = 'private' WHERE id = %s", (story["id"],))
    res = client.put(
        f"{URL}/{story['id']}", json=_body("제목", "가" * 3000, [("서윤", "상세")]), headers=OWNER
    )
    assert res.status_code == 422
    assert "BASIC_TEXT_TOO_LONG" in {i["code"] for i in res.json()["issues"]}
    assert client.get(f"{URL}/{story['id']}", headers=OWNER).json()["world_description"] == "세계관"


def test_emoji_counts_as_one_char_CREATE_US_01_AC09(client: TestClient) -> None:
    """글자 수는 코드 포인트 기준(이휘 확정). 이모지 1개 = 1자."""
    body = _body("제목", "😀" * (3000 - FIXED - 10), [("서윤", "상세상세상세상세상세")])
    assert "BASIC_TEXT_TOO_LONG" not in _codes(_create(client, body))


# ---- AC14: 순서 변경 저장·재조회 (드래그 결과 = 목록 순서) ----


def test_reordered_characters_are_restored_CREATE_US_01_AC14(client: TestClient) -> None:
    story = _create(client, _body("제목", "세계관", [("A", "a"), ("B", "b"), ("C", "c")]))
    a, b, c = story["characters"]
    body = {
        **_body("제목", "세계관", []),
        "characters": [
            {"id": c["id"], "name": "C", "description": "c"},
            {"id": a["id"], "name": "A", "description": "a"},
            {"id": b["id"], "name": "B", "description": "b"},
        ],
    }
    client.put(f"{URL}/{story['id']}", json=body, headers=OWNER)

    restored = client.get(f"{URL}/{story['id']}", headers=OWNER).json()["characters"]
    assert [(x["id"], x["name"], x["description"], x["position"]) for x in restored] == [
        (c["id"], "C", "c", 0),
        (a["id"], "A", "a", 1),
        (b["id"], "B", "b", 2),
    ]
