"""CREATE-US-07-AC10 스토리 소프트 삭제 API(DELETE /creation/stories/{story_id}).

실제 DB와 실제 인증을 쓰지 않는다. `get_conn`은 가짜 커넥션으로, `get_current_user_id`는
고정 사용자로 덮어쓴다. TestClient를 컨텍스트 없이 만들어 lifespan(DB 풀 열기)도 돌지 않는다.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_id
from app.core.config import settings
from app.db.session import get_conn
from app.main import create_app

OWNER = 7
OTHER = 8
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
PROBLEM_JSON = "application/problem+json"


class FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class FakeConn:
    """soft_delete_story가 보내는 UPDATE 하나만 해석하는 가짜 커넥션. 다른 속성은 없다."""

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


def _story(story_id: int, owner_id: int = OWNER, deleted_at: datetime | None = None) -> dict:
    return {"id": story_id, "owner_id": owner_id, "deleted_at": deleted_at}


@pytest.fixture
def fake_conn() -> FakeConn:
    return FakeConn(
        [
            _story(41),
            _story(42, owner_id=OTHER),
            _story(43, deleted_at=datetime(2026, 1, 1, tzinfo=UTC)),
        ]
    )


@pytest.fixture
def client(fake_conn: FakeConn) -> Iterator[TestClient]:
    app = create_app()

    def _fake_get_conn() -> Iterator[FakeConn]:
        yield fake_conn

    app.dependency_overrides[get_conn] = _fake_get_conn
    app.dependency_overrides[get_current_user_id] = lambda: OWNER
    # with 블록을 쓰지 않아 lifespan(open_pool)이 실행되지 않는다.
    yield TestClient(app)
    app.dependency_overrides.clear()


def _url(story_id: object) -> str:
    return f"{settings.api_prefix}/creation/stories/{story_id}"


def _assert_not_found(response) -> None:
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["status"] == 404
    assert body["code"] == "STORY_NOT_FOUND"


def test_ac10_delete_own_story_returns_204_empty(client: TestClient, fake_conn: FakeConn) -> None:
    """CREATE-US-07-AC10: 본인 스토리를 삭제하면 204와 빈 본문을 돌려주고 deleted_at만 기록한다."""
    response = client.delete(_url(41))

    assert response.status_code == 204
    assert response.content == b""
    assert fake_conn.stories[41]["deleted_at"] == NOW
    # (story_id, owner_id) 순서로 전달돼야 한다. 인자 순서가 뒤바뀌면 다른 행을 건드린다.
    assert [params for _, params in fake_conn.calls] == [(41, OWNER)]


def test_ac10_delete_other_owner_story_is_404(client: TestClient, fake_conn: FakeConn) -> None:
    """CREATE-US-07-AC10: 타인 스토리는 존재를 드러내지 않고 404 problem+json."""
    _assert_not_found(client.delete(_url(42)))
    assert fake_conn.stories[42]["deleted_at"] is None


def test_ac10_delete_missing_story_is_404(client: TestClient) -> None:
    """CREATE-US-07-AC10: 없는 스토리는 404 problem+json."""
    _assert_not_found(client.delete(_url(999)))


def test_ac10_delete_already_deleted_story_is_404(client: TestClient, fake_conn: FakeConn) -> None:
    """CREATE-US-07-AC10: 이미 삭제된 스토리는 404이고 최초 삭제 시각을 유지한다."""
    first = fake_conn.stories[43]["deleted_at"]
    _assert_not_found(client.delete(_url(43)))
    assert fake_conn.stories[43]["deleted_at"] == first


def test_ac10_repeat_delete_is_404_after_first_success(
    client: TestClient, fake_conn: FakeConn
) -> None:
    """CREATE-US-07-AC10: 두 번째 삭제 요청은 404이며 삭제 시각은 바뀌지 않는다."""
    assert client.delete(_url(41)).status_code == 204
    _assert_not_found(client.delete(_url(41)))
    assert fake_conn.stories[41]["deleted_at"] == NOW


@pytest.mark.parametrize("bad_id", ["0", "-1", "abc", "9223372036854775808"])
def test_ac10_invalid_story_id_is_422_problem(
    client: TestClient, fake_conn: FakeConn, bad_id: str
) -> None:
    """CREATE-US-07-AC10: 경로 검증은 기존 StoryId 규칙을 따르며 DB에 닿지 않는다."""
    response = client.delete(_url(bad_id))

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_conn.calls == []


def test_existing_save_visibility_routes_unchanged(client: TestClient) -> None:
    """DELETE 추가 후에도 기존 /creation 조회·저장 경로가 그대로 있다."""
    paths = client.app.openapi()["paths"]
    prefix = f"{settings.api_prefix}/creation/stories/{{story_id}}"
    assert {"get", "put"} <= paths[f"{prefix}/creation"].keys()
    assert "delete" in paths[prefix]
