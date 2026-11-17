"""CREATE-US-01 캐릭터 피처: AC04·AC05·AC08·AC10·AC11·AC12."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.features.creation.common.names import name_compare_key

URL = "/api/v1/creation/stories"
OWNER = {"X-User-Id": "1"}
OTHER = {"X-User-Id": "2"}

BASIC = {"title": "달빛 서점", "subtitle": "한 줄 소개", "world_description": "세계관"}


def _char(name: str | None, description: str | None = "상세", id: int | None = None) -> dict:
    item: dict = {"name": name, "description": description}
    if id is not None:
        item["id"] = id
    return item


def _create(client: TestClient, characters: list[dict], headers: dict = OWNER) -> dict:
    res = client.post(URL, json={**BASIC, "characters": characters}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def _put(client: TestClient, sid: int, characters: list[dict]) -> dict:
    res = client.put(f"{URL}/{sid}", json={**BASIC, "characters": characters}, headers=OWNER)
    assert res.status_code == 200, res.text
    return res.json()


def _issues(story: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for i in story["issues"]:
        out.setdefault(i["field"], set()).add(i["code"])
    return out


# ---- AC04: 캐릭터별 이름·상세 저장·복원, 섞이지 않음 ----


def test_characters_saved_and_restored_without_mixing_CREATE_US_01_AC04(
    client: TestClient,
) -> None:
    chars = [_char("서윤", "서점 주인.\n말수가 적다."), _char("Alex", "단골 손님")]
    sid = _create(client, chars)["id"]

    story = client.get(f"{URL}/{sid}", headers=OWNER).json()
    assert [(c["name"], c["description"], c["position"]) for c in story["characters"]] == [
        ("서윤", "서점 주인.\n말수가 적다.", 0),
        ("Alex", "단골 손님", 1),
    ]
    assert story["issues"] == []


def test_update_keeps_character_ids_CREATE_US_01_AC04(client: TestClient) -> None:
    story = _create(client, [_char("서윤"), _char("Alex")])
    created = story["characters"]
    body = [_char(c["name"], c["description"] + " 수정", id=c["id"]) for c in created]
    updated = _put(client, story["id"], body)
    assert [c["id"] for c in updated["characters"]] == [c["id"] for c in created]
    assert [c["description"] for c in updated["characters"]] == ["상세 수정", "상세 수정"]


# ---- AC05: 이름·상세 필수, 입력 보존 ----


@pytest.mark.parametrize(
    ("name", "description", "field"),
    [
        (None, "상세", "name"),
        ("", "상세", "name"),
        ("서윤", "", "description"),
        ("서윤", " ", "description"),
    ],
)
def test_blank_name_or_description_is_reported_and_kept_CREATE_US_01_AC05(
    client: TestClient, name: str | None, description: str, field: str
) -> None:
    story = _create(client, [_char(name, description)])
    assert _issues(story) == {f"characters[0].{field}": {"REQUIRED"}}
    saved = client.get(f"{URL}/{story['id']}", headers=OWNER).json()["characters"][0]
    assert (saved["name"], saved["description"]) == (name, description)


# ---- AC08: 1~10명 ----


@pytest.mark.parametrize("count", [0, 11])
def test_character_count_out_of_range_CREATE_US_01_AC08(client: TestClient, count: int) -> None:
    story = _create(client, [_char(f"인물{i}") for i in range(count)])
    assert _issues(story) == {"characters": {"CHARACTER_COUNT_OUT_OF_RANGE"}}
    assert len(story["characters"]) == count  # 임시 저장은 범위를 벗어나도 그대로 보존


@pytest.mark.parametrize("count", [1, 10])
def test_character_count_in_range_CREATE_US_01_AC08(client: TestClient, count: int) -> None:
    story = _create(client, [_char(f"인물{i}") for i in range(count)])
    assert story["issues"] == []


# ---- AC10: 대소문자·띄어쓰기 무시 중복, 원래 표기 유지 ----


@pytest.mark.parametrize(
    ("a", "b"), [("Alex", "alex"), ("이도형", "이 도형"), ("Mary Jane", "maryjane")]
)
def test_duplicate_names_ignoring_case_and_spaces_CREATE_US_01_AC10(
    client: TestClient, a: str, b: str
) -> None:
    story = _create(client, [_char(a), _char(b)])
    assert _issues(story) == {
        "characters[0].name": {"NAME_DUPLICATE"},
        "characters[1].name": {"NAME_DUPLICATE"},
    }
    assert [c["name"] for c in story["characters"]] == [a, b]


def test_name_compare_key_CREATE_US_01_AC10() -> None:
    assert name_compare_key("Alex") == name_compare_key("alex") == "alex"
    assert name_compare_key("이 도형") == name_compare_key("이도형")
    assert name_compare_key("서윤") != name_compare_key("서연")


# ---- AC11: 앞뒤 띄어쓰기는 형식 오류, 자르지 않음 ----


@pytest.mark.parametrize("name", [" 서윤", "서윤 ", " 서윤 "])
def test_surrounding_space_is_format_error_and_not_trimmed_CREATE_US_01_AC11(
    client: TestClient, name: str
) -> None:
    story = _create(client, [_char(name)])
    assert _issues(story) == {"characters[0].name": {"NAME_SURROUNDING_SPACE"}}
    assert story["characters"][0]["name"] == name


def test_inner_space_is_allowed_CREATE_US_01_AC11(client: TestClient) -> None:
    story = _create(client, [_char("이 도형")])
    assert story["issues"] == []


# ---- AC12: 추가·삭제 후 남은 인물 보존, 인원·필수 재검사 ----


def test_add_and_delete_keeps_remaining_characters_CREATE_US_01_AC12(client: TestClient) -> None:
    story = _create(client, [_char("A", "a"), _char("B", "b"), _char("C", "c")])
    a, _, c = story["characters"]

    updated = _put(
        client,
        story["id"],
        [_char("C", "c", id=c["id"]), _char("A", "a", id=a["id"]), _char("D", "d")],
    )
    got = [
        (ch["id"], ch["name"], ch["description"], ch["position"]) for ch in updated["characters"]
    ]
    assert got[:2] == [(c["id"], "C", "c", 0), (a["id"], "A", "a", 1)]
    assert got[2][1:] == ("D", "d", 2)
    assert updated["issues"] == []


def test_deleting_down_to_zero_rechecks_count_CREATE_US_01_AC12(client: TestClient) -> None:
    story = _create(client, [_char("A")])
    updated = _put(client, story["id"], [])
    assert updated["characters"] == []
    assert _issues(updated) == {"characters": {"CHARACTER_COUNT_OUT_OF_RANGE"}}


def test_omitting_characters_keeps_them(client: TestClient) -> None:
    story = _create(client, [_char("A")])
    res = client.put(f"{URL}/{story['id']}", json=BASIC, headers=OWNER)
    assert [c["name"] for c in res.json()["characters"]] == ["A"]


def test_deleting_character_removes_its_linked_rows(
    client: TestClient, db: psycopg.Connection
) -> None:
    """삭제한 캐릭터의 연결(초기 호감도 등)은 DB CASCADE로 함께 정리된다."""
    story = _create(client, [_char("A"), _char("B")])
    a, b = story["characters"]
    db.execute(
        """
        INSERT INTO character_initial_affinities
            (source_character_id, target_kind, target_character_id, affinity_input)
        VALUES (%s, 'character', %s, '10')
        """,
        (a["id"], b["id"]),
    )
    _put(client, story["id"], [_char("A", id=a["id"])])
    left = db.execute("SELECT count(*) AS n FROM character_initial_affinities").fetchone()
    assert left["n"] == 0


# ---- 잘못된 id·완성 저장 ----


def test_unknown_or_foreign_character_id_is_rejected(client: TestClient) -> None:
    mine = _create(client, [_char("A")])
    other = _create(client, [_char("B")], headers=OTHER)
    foreign_id = other["characters"][0]["id"]

    res = client.put(
        f"{URL}/{mine['id']}",
        json={**BASIC, "characters": [_char("X", id=foreign_id)]},
        headers=OWNER,
    )
    assert res.status_code == 422
    assert res.json()["code"] == "INVALID_CHARACTER_REFERENCE"
    assert [
        c["name"] for c in client.get(f"{URL}/{mine['id']}", headers=OWNER).json()["characters"]
    ] == ["A"]


def test_duplicate_id_in_list_is_rejected(client: TestClient) -> None:
    story = _create(client, [_char("A")])
    cid = story["characters"][0]["id"]
    res = client.put(
        f"{URL}/{story['id']}",
        json={**BASIC, "characters": [_char("A", id=cid), _char("B", id=cid)]},
        headers=OWNER,
    )
    assert res.status_code == 422


def test_create_rejects_character_ids(client: TestClient) -> None:
    res = client.post(URL, json={**BASIC, "characters": [_char("A", id=1)]}, headers=OWNER)
    assert res.status_code == 422
    assert res.json()["code"] == "INVALID_CHARACTER_REFERENCE"


def test_completed_story_rejects_invalid_characters(
    client: TestClient, db: psycopg.Connection
) -> None:
    story = _create(client, [_char("A")])
    db.execute("UPDATE stories SET save_state = 'private' WHERE id = %s", (story["id"],))
    res = client.put(f"{URL}/{story['id']}", json={**BASIC, "characters": []}, headers=OWNER)
    assert res.status_code == 422
    assert res.json()["code"] == "STORY_INCOMPLETE"
    assert len(client.get(f"{URL}/{story['id']}", headers=OWNER).json()["characters"]) == 1
