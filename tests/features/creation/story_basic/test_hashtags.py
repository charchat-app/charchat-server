"""CREATE-US-01 해시태그 피처: AC13·AC15·AC16·AC17·AC18."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.features.creation.story_basic.schemas import MAX_TAGS_PER_REQUEST

URL = "/api/v1/creation/stories"
TAGS = "/api/v1/creation/tags"
OWNER = {"X-User-Id": "1"}
OTHER = {"X-User-Id": "2"}

VALID = {
    "title": "달빛 서점",
    "subtitle": "한 줄 소개",
    "world_description": "세계관",
    "characters": [{"name": "서윤", "description": "서점 주인"}],
}


def _create(client: TestClient, tags: list[str], headers: dict = OWNER) -> dict:
    res = client.post(URL, json={**VALID, "tags": tags}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def _publish(db: psycopg.Connection, story_id: int, state: str = "public") -> None:
    """공개 전환 API(CREATE-US-07)가 아직 없어 테스트에서 직접 상태를 바꾼다."""
    db.execute("UPDATE stories SET save_state = %s WHERE id = %s", (state, story_id))


def _issues(story: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for i in story["issues"]:
        out.setdefault(i["field"], set()).add(i["code"])
    return out


# ---- AC13: 태그당 최대 10자, 원문 보존 ----


def test_tag_up_to_10_chars_is_valid_CREATE_US_01_AC13(client: TestClient) -> None:
    story = _create(client, ["가나다라마바사아자차", "로맨스"])  # 10자, 3자
    assert story["issues"] == []


def test_tag_over_10_chars_is_reported_and_kept_CREATE_US_01_AC13(client: TestClient) -> None:
    long_tag = "가나다라마바사아자차카"  # 11자
    story = _create(client, ["로맨스", long_tag])
    assert _issues(story) == {f"tags[{story['tags'].index(long_tag)}]": {"TAG_TOO_LONG"}}
    # 임시 저장은 잘라내지 않고 원문 그대로 보존한다
    reloaded = client.get(f"{URL}/{story['id']}", headers=OWNER).json()
    assert long_tag in reloaded["tags"]


def test_tag_length_counts_spaces_CREATE_US_01_AC13(client: TestClient) -> None:
    story = _create(client, ["판타지 로맨스 코미디"])  # 공백 포함 11자
    assert _issues(story) == {"tags[0]": {"TAG_TOO_LONG"}}


def test_blank_tag_is_reported(client: TestClient) -> None:
    story = _create(client, ["  "])
    assert _issues(story) == {"tags[0]": {"TAG_BLANK"}}


def test_completed_story_rejects_long_tag_CREATE_US_01_AC13(
    client: TestClient, db: psycopg.Connection
) -> None:
    story = _create(client, ["로맨스"])
    _publish(db, story["id"], "private")
    res = client.put(
        f"{URL}/{story['id']}", json={**VALID, "tags": ["가나다라마바사아자차카"]}, headers=OWNER
    )
    assert res.status_code == 422
    assert res.json()["code"] == "STORY_INCOMPLETE"
    assert client.get(f"{URL}/{story['id']}", headers=OWNER).json()["tags"] == ["로맨스"]


# ---- AC15: 최대 10개, 길이 제한과 별개 ----


@pytest.mark.parametrize(("count", "exceeded"), [(10, False), (11, True)])
def test_tag_count_limit_CREATE_US_01_AC15(client: TestClient, count: int, exceeded: bool) -> None:
    story = _create(client, [f"태그{i}" for i in range(count)])
    assert ("tags" in _issues(story)) is exceeded
    assert len(story["tags"]) == count  # 초과해도 잘라내지 않음


def test_count_and_length_are_reported_separately_CREATE_US_01_AC15(client: TestClient) -> None:
    tags = [f"태그{i}" for i in range(10)] + ["가나다라마바사아자차카"]
    story = _create(client, tags)
    codes = {c for cs in _issues(story).values() for c in cs}
    assert codes == {"TAG_COUNT_EXCEEDED", "TAG_TOO_LONG"}


def test_tags_over_request_limit_are_rejected(client: TestClient) -> None:
    res = client.post(
        URL,
        json={**VALID, "tags": [f"t{i}" for i in range(MAX_TAGS_PER_REQUEST + 1)]},
        headers=OWNER,
    )
    assert res.status_code == 422
    assert res.json()["code"] == "VALIDATION_ERROR"


# ---- AC17: 한 글자 자동검색, 공개 스토리 수, 많은 순 ----


def test_one_char_prefix_suggests_with_public_counts_CREATE_US_01_AC17(
    client: TestClient, db: psycopg.Connection
) -> None:
    for tags in (["로맨스", "판타지"], ["로맨스", "로판"], ["로맨스"]):
        _publish(db, _create(client, tags)["id"])
    res = client.get(TAGS, params={"q": "로"}, headers=OWNER)
    assert res.status_code == 200
    assert res.json() == [
        {"name": "로맨스", "public_story_count": 3},
        {"name": "로판", "public_story_count": 1},
    ]


def test_private_draft_and_deleted_are_not_counted_CREATE_US_01_AC17(
    client: TestClient, db: psycopg.Connection
) -> None:
    _publish(db, _create(client, ["로맨스"])["id"])
    _publish(db, _create(client, ["로맨스"])["id"], "private")
    _create(client, ["로맨스", "로코"])  # draft
    deleted = _create(client, ["로맨스"])["id"]
    _publish(db, deleted)
    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (deleted,))

    res = client.get(TAGS, params={"q": "로"}, headers=OWNER).json()
    # 공개 1개만 센다. 공개 스토리가 없는 '로코'는 후보에서 뺀다
    assert res == [{"name": "로맨스", "public_story_count": 1}]


def test_prefix_match_only_and_case_insensitive_CREATE_US_01_AC17(
    client: TestClient, db: psycopg.Connection
) -> None:
    _publish(db, _create(client, ["판타지로맨스", "Romance"])["id"])
    assert client.get(TAGS, params={"q": "로"}, headers=OWNER).json() == []
    assert [t["name"] for t in client.get(TAGS, params={"q": "ro"}, headers=OWNER).json()] == [
        "Romance"
    ]


def test_like_wildcards_are_literal_CREATE_US_01_AC17(
    client: TestClient, db: psycopg.Connection
) -> None:
    _publish(db, _create(client, ["100%", "1000", "a_b", "axb", "느낌!표"])["id"])

    def names(q: str) -> list[str]:
        return [t["name"] for t in client.get(TAGS, params={"q": q}, headers=OWNER).json()]

    assert names("100%") == ["100%"]
    assert names("a_") == ["a_b"]
    assert names("느낌!") == ["느낌!표"]


def test_empty_query_is_rejected(client: TestClient) -> None:
    assert client.get(TAGS, params={"q": ""}, headers=OWNER).status_code == 422


# ---- AC18: 검색에 없는 태그도 직접 추가 ----


def test_new_tag_not_in_suggestions_can_be_added_CREATE_US_01_AC18(
    client: TestClient, db: psycopg.Connection
) -> None:
    assert client.get(TAGS, params={"q": "서점"}, headers=OWNER).json() == []
    story = _create(client, ["서점물"])
    assert story["tags"] == ["서점물"]
    assert story["issues"] == []
    _publish(db, story["id"])
    assert client.get(TAGS, params={"q": "서점"}, headers=OWNER).json() == [
        {"name": "서점물", "public_story_count": 1}
    ]


def test_new_and_existing_tags_share_10_limit_CREATE_US_01_AC18(
    client: TestClient, db: psycopg.Connection
) -> None:
    _publish(db, _create(client, ["로맨스"])["id"])
    story = _create(client, ["로맨스"] + [f"새태그{i}" for i in range(10)])
    assert "tags" in _issues(story)


# ---- AC16: 해시태그로 공개 스토리 찾기 ----


def test_search_public_stories_by_tag_CREATE_US_01_AC16(
    client: TestClient, db: psycopg.Connection
) -> None:
    first = _create(client, ["로맨스", "판타지"])["id"]
    second = _create(client, ["로맨스"], headers=OTHER)["id"]
    private = _create(client, ["로맨스"])["id"]
    draft = _create(client, ["로맨스"])["id"]
    other_tag = _create(client, ["판타지"])["id"]
    for sid in (first, second, other_tag):
        _publish(db, sid)
    _publish(db, private, "private")

    res = client.get(f"{TAGS}/로맨스/stories", headers=OWNER)
    assert res.status_code == 200
    body = res.json()
    assert [s["id"] for s in body] == [second, first]  # 최근 저장 순, 다른 사용자 공개분 포함
    assert set(body[1]["tags"]) == {"로맨스", "판타지"}
    assert draft not in [s["id"] for s in body]


def test_search_matches_exact_tag_only_CREATE_US_01_AC16(
    client: TestClient, db: psycopg.Connection
) -> None:
    _publish(db, _create(client, ["로맨스판타지"])["id"])
    assert client.get(f"{TAGS}/로맨스/stories", headers=OWNER).json() == []


# ---- 태그 동일성: 대소문자·띄어쓰기는 같은 태그로 묶는다 (저장은 원문) ----


def test_suggestions_merge_case_and_space_variants_CREATE_US_01_AC17(
    client: TestClient, db: psycopg.Connection
) -> None:
    for tags in (["Romance"], ["Romance"], ["romance"], ["판타지 로맨스"], ["판타지로맨스"]):
        _publish(db, _create(client, tags)["id"])

    res = client.get(TAGS, params={"q": "ROM"}, headers=OWNER).json()
    # 한 줄로 묶고 공개 스토리 3개, 대표 표기는 가장 많이 쓴 'Romance'
    assert res == [{"name": "Romance", "public_story_count": 3}]

    res = client.get(TAGS, params={"q": "판타지 로"}, headers=OWNER).json()
    assert [r["public_story_count"] for r in res] == [2]
    assert res[0]["name"] in {"판타지 로맨스", "판타지로맨스"}


def test_story_is_counted_once_per_key_CREATE_US_01_AC17(
    client: TestClient, db: psycopg.Connection
) -> None:
    """한 스토리에 두 표기가 같이 있어도(임시 저장 후 공개된 예외 상황) 한 번만 센다."""
    _publish(db, _create(client, ["Romance", "romance"])["id"])
    assert client.get(TAGS, params={"q": "ro"}, headers=OWNER).json()[0]["public_story_count"] == 1


def test_blank_query_after_normalization_returns_nothing(client: TestClient) -> None:
    assert client.get(TAGS, params={"q": "   "}, headers=OWNER).json() == []


def test_search_ignores_case_and_spaces_CREATE_US_01_AC16(
    client: TestClient, db: psycopg.Connection
) -> None:
    upper = _create(client, ["Romance"])["id"]
    lower = _create(client, ["romance"])["id"]
    spaced = _create(client, ["판타지 로맨스"])["id"]
    for sid in (upper, lower, spaced):
        _publish(db, sid)

    found = [s["id"] for s in client.get(f"{TAGS}/romance/stories", headers=OWNER).json()]
    assert sorted(found) == sorted([upper, lower])
    found = [s["id"] for s in client.get(f"{TAGS}/판타지로맨스/stories", headers=OWNER).json()]
    assert found == [spaced]


def test_each_story_keeps_its_own_spelling(client: TestClient, db: psycopg.Connection) -> None:
    a = _create(client, ["Romance"])
    b = _create(client, ["romance"])
    assert client.get(f"{URL}/{a['id']}", headers=OWNER).json()["tags"] == ["Romance"]
    assert client.get(f"{URL}/{b['id']}", headers=OWNER).json()["tags"] == ["romance"]


def test_same_key_twice_in_one_story_is_reported(client: TestClient) -> None:
    story = _create(client, ["Romance", "로맨스", "romance"])
    assert _issues(story) == {f"tags[{story['tags'].index('romance')}]": {"TAG_DUPLICATE"}}
    assert set(story["tags"]) == {"Romance", "로맨스", "romance"}  # 원문은 보존
