"""CREATE-US-07-AC10 스토리 소프트 삭제 유스케이스. 가짜 커넥션만 쓰며 실제 DB를 사용하지 않는다."""

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from app.features.creation.save_visibility import soft_delete
from app.features.creation.save_visibility.soft_delete import (
    SoftDeleteResult,
    soft_delete_story,
)
from app.features.creation.story_basic.service import StoryNotFoundError

OWNER = 1
OTHER = 2
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class FakeConn:
    """stories 행을 메모리에 두고, soft_delete가 보내는 UPDATE 하나만 해석하는 가짜 커넥션.

    execute 외의 속성(commit·close·transaction 등)을 쓰면 AttributeError가 나므로
    유스케이스가 호출자의 트랜잭션·커넥션을 건드리지 않는지도 함께 확인된다.
    """

    def __init__(self, stories: list[dict[str, Any]]) -> None:
        self.stories = {s["id"]: s for s in stories}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        self.calls.append((query, params))
        story_id, owner_id = params
        row = self.stories.get(story_id)
        if row is None or row["owner_id"] != owner_id or row["deleted_at"] is not None:
            return FakeCursor(None)
        row["deleted_at"] = NOW
        return FakeCursor({"id": row["id"], "deleted_at": row["deleted_at"]})


def _story(story_id: int, owner_id: int = OWNER, state: str = "draft", **extra: Any) -> dict:
    return {
        "id": story_id,
        "owner_id": owner_id,
        "save_state": state,
        "title": "달빛 서점",
        "deleted_at": None,
        **extra,
    }


def _sql(conn: FakeConn) -> str:
    return re.sub(r"\s+", " ", conn.calls[0][0]).strip()


@pytest.mark.parametrize("state", ["draft", "private", "public"])
def test_owner_soft_deletes_story_in_any_save_state_CREATE_US_07_AC10(state: str) -> None:
    conn = FakeConn([_story(10, state=state)])

    result = soft_delete_story(conn, OWNER, 10)  # type: ignore[arg-type]

    assert result == SoftDeleteResult(story_id=10, deleted_at=NOW)
    assert conn.stories[10]["deleted_at"] == NOW
    # 공개 범위는 삭제와 별개라 바꾸지 않고, 행·자식 데이터는 그대로 남긴다.
    assert conn.stories[10]["save_state"] == state
    assert conn.stories[10]["title"] == "달빛 서점"
    assert len(conn.stories) == 1


def test_uses_single_atomic_parameterized_update_CREATE_US_07_AC10() -> None:
    story_id = 987654321
    conn = FakeConn([_story(story_id)])

    soft_delete_story(conn, OWNER, story_id)  # type: ignore[arg-type]

    assert len(conn.calls) == 1
    sql = _sql(conn)
    assert sql.startswith("UPDATE stories SET deleted_at =")
    # 소유자와 미삭제 조건이 같은 문장의 WHERE에 있어 확인과 변경 사이에 틈이 없다.
    assert "WHERE id = %s AND owner_id = %s AND deleted_at IS NULL" in sql
    assert conn.calls[0][1] == (story_id, OWNER)
    assert str(story_id) not in sql and str(OWNER) not in sql


def test_never_physically_deletes_or_touches_children_CREATE_US_07_AC10() -> None:
    conn = FakeConn([_story(10, state="public")])

    soft_delete_story(conn, OWNER, 10)  # type: ignore[arg-type]

    sql = _sql(conn).upper()
    # DELETED_AT 컬럼명에 부분 일치하지 않도록 SQL 토큰 단위로만 DELETE·TRUNCATE를 찾는다.
    assert re.findall(r"\b(?:DELETE|TRUNCATE)\b", sql) == []
    assert sql.startswith("UPDATE STORIES SET DELETED_AT =")
    assert "SET DELETED_AT =" in sql
    assert "SAVE_STATE" not in sql
    # stories 외의 테이블(자식·세션·이미지 연결)은 건드리지 않는다.
    assert re.findall(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(\w+)", sql) == ["STORIES"]


@pytest.mark.parametrize(
    ("stories", "caller", "story_id"),
    [
        pytest.param([], OWNER, 10, id="missing"),
        pytest.param([_story(10, owner_id=OTHER)], OWNER, 10, id="other_owner"),
        pytest.param([_story(10, deleted_at=NOW)], OWNER, 10, id="already_deleted"),
    ],
)
def test_unavailable_story_raises_same_not_found_CREATE_US_07_AC10(
    stories: list[dict], caller: int, story_id: int
) -> None:
    conn = FakeConn(stories)
    before = {k: dict(v) for k, v in conn.stories.items()}

    with pytest.raises(StoryNotFoundError) as exc:
        soft_delete_story(conn, caller, story_id)  # type: ignore[arg-type]

    # 없음·타인·이미 삭제를 구분할 수 없어야 하고, 기존 스토리 오류와 같은 응답이어야 한다.
    assert exc.value.status == 404
    assert exc.value.code == "STORY_NOT_FOUND"
    assert exc.value.detail == f"스토리 {story_id}를 찾을 수 없습니다."
    assert conn.stories == before


def test_other_owner_cannot_delete_and_owner_still_can_CREATE_US_07_AC10() -> None:
    conn = FakeConn([_story(10)])

    with pytest.raises(StoryNotFoundError):
        soft_delete_story(conn, OTHER, 10)  # type: ignore[arg-type]
    assert conn.stories[10]["deleted_at"] is None

    assert soft_delete_story(conn, OWNER, 10).story_id == 10  # type: ignore[arg-type]


def test_second_delete_is_not_found_and_keeps_first_timestamp_CREATE_US_07_AC10() -> None:
    conn = FakeConn([_story(10)])
    soft_delete_story(conn, OWNER, 10)  # type: ignore[arg-type]

    with pytest.raises(StoryNotFoundError):
        soft_delete_story(conn, OWNER, 10)  # type: ignore[arg-type]
    assert conn.stories[10]["deleted_at"] == NOW


def test_provides_no_restore_path_CREATE_US_07_AC10() -> None:
    public_names = [n for n in dir(soft_delete) if not n.startswith("_")]
    assert not [n for n in public_names if re.search(r"restore|undelete|recover", n, re.I)]
