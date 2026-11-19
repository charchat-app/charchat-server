"""CREATE-US-02 테스트 픽스처. 팀 테스트 DB(`TEST_DATABASE_URL`)의 공통 `db`·`db_url`을 쓴다."""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_id
from app.core.config import settings
from app.main import create_app

OWNER_ID = 100


@pytest.fixture
def context(db, db_url, monkeypatch):
    """(client, app, story, owner_id, dsn). `db` 픽스처가 테스트마다 모든 테이블을 비운다."""
    # create_app은 전역 settings의 CORS 값을 읽는다. 검사용 origin만 바꾼다.
    monkeypatch.setattr(settings, "cors_origins", ["http://localhost:8089"])
    app = create_app(db_url)
    # 사용자 구분은 공통 X-User-Id 의존성을 그대로 두고, 검사에서는 소유자만 고정한다.
    app.dependency_overrides[get_current_user_id] = lambda: OWNER_ID
    story = db.execute(
        """INSERT INTO stories(owner_id,title,world_description) VALUES (%s,%s,%s)
           RETURNING id,saved_at""",
        (OWNER_ID, "가상 스토리", "수정하면 안 되는 세계관"),
    ).fetchone()
    with TestClient(app) as client:
        yield client, app, story, OWNER_ID, db_url
