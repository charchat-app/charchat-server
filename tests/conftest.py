"""공통 테스트 픽스처.

DB 테스트는 `TEST_DATABASE_URL`(이름이 `_test`로 끝나는 DB)에서만 돈다.
세션 시작 시 public 스키마를 지우고 `app/db/schema.sql`로 다시 만들며, 테스트마다 데이터를 비운다.
설정이 없으면 DB 테스트는 건너뛴다. 공용 DB를 가리키지 않는다.

CREATE-US-08-AC02: `.env`나 셸에 `RATING_REVIEW_ENABLED=true`가 있어도 일반 pytest는 등급 검토를
끈 채로 시작한다(픽스처 내용을 외부 평가기로 보내지 않는다). `app.main`은 import 때 기본 앱을
만들므로 앱을 import하기 전에 환경변수로 덮어쓴다(환경변수가 `.env`보다 우선한다). 켜야 하는
테스트는 가짜 전송과 함께 그 테스트 범위에서만 명시적으로 켠다(`test_rating_runtime.py`).
"""

import os

os.environ["RATING_REVIEW_ENABLED"] = "false"

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.core.config import Settings, settings  # noqa: E402
from app.main import create_app  # noqa: E402

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "app" / "db" / "schema.sql"
# schema.sql이 넣는 기준 데이터 표. 테스트마다 비우지 않는다.
SEED_TABLES = ("models", "categories")


def _ensure_database(url: str) -> None:
    dbname = conninfo_to_dict(url).get("dbname", "")
    admin_url = make_conninfo(url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


@pytest.fixture(scope="session", autouse=True)
def dev_user_header_code_default():
    """BASE-03: .env에 AUTH_DEV_HEADER_ENABLED=true가 있어도 코드 기본값(꺼짐)에서 시작한다."""
    previous = settings.auth_dev_header_enabled
    settings.auth_dev_header_enabled = Settings.model_fields["auth_dev_header_enabled"].default
    yield
    settings.auth_dev_header_enabled = previous


@pytest.fixture
def dev_user_header_enabled():
    """BASE-03: 개발용 X-User-Id 헤더를 켠다(코드 기본값은 꺼짐). 자동 적용하지 않는다.

    헤더로 사용자를 정하는 기존 테스트 폴더의 conftest가 autouse로 요청하고,
    그 밖의 테스트는 필요할 때만 인자로 받는다. 나머지는 기본값(꺼짐) 그대로 돈다.
    테스트 안의 monkeypatch.undo()에 풀리지 않도록 monkeypatch를 쓰지 않는다.
    """
    previous = settings.auth_dev_header_enabled
    settings.auth_dev_header_enabled = True
    yield
    settings.auth_dev_header_enabled = previous


@pytest.fixture(scope="session")
def db_url() -> str:
    url = settings.test_database_url
    if not url:
        pytest.skip("TEST_DATABASE_URL이 없어 DB 테스트를 건너뜁니다.")
    dbname = conninfo_to_dict(url).get("dbname", "")
    if not dbname.endswith("_test"):
        pytest.exit(f"TEST_DATABASE_URL의 DB 이름은 _test로 끝나야 합니다: {dbname}", returncode=2)
    _ensure_database(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    return url


@pytest.fixture
def db(db_url: str) -> Iterator[psycopg.Connection]:
    """테스트 준비·확인용 직접 연결. 테스트 전에 모든 테이블을 비운다.

    기준 데이터 표(models, categories)는 비우지 않는다. 바꾼 테스트가 직접 되돌린다.
    """
    with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> ALL(%s)",
            (list(SEED_TABLES),),
        ).fetchall()
        names = sql.SQL(", ").join(sql.Identifier(t["tablename"]) for t in tables)
        conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(names))
        yield conn


@pytest.fixture
def client(db: psycopg.Connection, db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(db_url)) as c:
        yield c
