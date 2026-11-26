"""CREATE-US-02 테스트 픽스처. 공통 `db_url`·`db`·`client`(tests/conftest.py)를 쓴다."""

import pytest

from app.api.deps import get_current_user_id
from app.main import create_app

OWNER_ID = 100


@pytest.fixture(autouse=True)
def dev_user_header(dev_user_header_enabled):
    """BASE-03: 이 폴더 테스트는 개발용 X-User-Id 헤더로 사용자를 정한다(tests/conftest.py)."""


def create_story(db):
    return db.execute(
        """INSERT INTO stories(owner_id,title,world_description) VALUES (%s,%s,%s)
           RETURNING id,saved_at""",
        (OWNER_ID, "가상 스토리", "수정하면 안 되는 세계관"),
    ).fetchone()


@pytest.fixture
def context(client, db, db_url):
    """(client, app, story, owner_id, dsn). `db` 픽스처가 테스트마다 모든 테이블을 비운다.

    사용자 구분은 dev 테스트와 같이 공통 의존성에 `X-User-Id` 헤더를 보내 정한다.
    인증 의존성을 덮어쓰지 않으며, 요청마다 헤더를 주면 그 값이 우선한다.
    """
    client.headers["X-User-Id"] = str(OWNER_ID)
    yield client, client.app, create_story(db), OWNER_ID, db_url


@pytest.fixture
def adapter_context(db, db_url):
    """(app, story, dsn). 실제 소켓 서버가 필요한 앱 어댑터 검사 전용.

    앱 검사 스크립트는 사용자 헤더를 보내지 않으므로, 이 검사의 앱에서만 소유자를 고정한다.
    """
    app = create_app(db_url)
    app.dependency_overrides[get_current_user_id] = lambda: OWNER_ID
    return app, create_story(db), db_url
