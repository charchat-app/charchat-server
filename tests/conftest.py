"""공통 테스트 픽스처.

DB 테스트는 `TEST_DATABASE_URL`(이름이 `_test`로 끝나는 DB)에서만 돈다.
세션 시작 시 public 스키마를 지우고 `app/db/schema.sql`로 다시 만들며, 테스트마다 데이터를 비운다.
설정이 없으면 DB 테스트는 건너뛴다. 공용 DB를 가리키지 않는다.
"""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app.core.config import settings
from app.main import create_app

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "app" / "db" / "schema.sql"
# schema.sql이 넣는 기준 데이터 표. 테스트마다 비우지 않는다.
SEED_TABLES = ("models",)


def _ensure_database(url: str) -> None:
    dbname = conninfo_to_dict(url).get("dbname", "")
    admin_url = make_conninfo(url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


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

    schema.sql이 넣는 기준 데이터 표(models)는 비우지 않는다. 바꾼 테스트가 직접 되돌린다.
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
