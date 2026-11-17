"""CREATE-US-02 격리 PostgreSQL. 공유 DB 환경변수를 사용하지 않는다."""

import itertools
import shutil
import subprocess
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.api.deps import get_current_user_id
from app.core.config import settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[4]
OWNERS = itertools.count(100)


@pytest.fixture(scope="session")
def local_dsn(tmp_path_factory):
    """새로운 전용 Unix socket 클러스터만 생성하고 종료한다."""
    pg_config = shutil.which("pg_config")
    if not pg_config:
        pytest.skip("로컬 PostgreSQL 실행 파일이 필요합니다.")
    bindir = Path(subprocess.check_output([pg_config, "--bindir"], text=True).strip())
    if not (bindir / "initdb").exists():
        pytest.skip("initdb가 필요합니다.")
    root = tmp_path_factory.mktemp("expression-pg")
    data, socket = root / "data", root / "socket"
    socket.mkdir(mode=0o700)
    subprocess.run(
        [
            str(bindir / "initdb"),
            "-D",
            str(data),
            "-U",
            "test_owner",
            "--no-locale",
            "--encoding=UTF8",
            "--auth-local=trust",
            "--auth-host=reject",
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    subprocess.run(
        [
            str(bindir / "pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-k {socket} -p 55449 -c listen_addresses='' -c shared_buffers=16MB",
            "-w",
            "-t",
            "10",
            "start",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    dsn = make_conninfo(host=str(socket), port=55449, user="test_owner", dbname="postgres")
    try:
        with psycopg.connect(dsn) as connection:
            connection.execute((ROOT / "app/db/schema.sql").read_text())
            connection.execute((ROOT / "migrations/001_expression_write_requests.sql").read_text())
        yield dsn
    finally:
        subprocess.run(
            [str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "-t", "10", "stop"],
            check=True,
            capture_output=True,
            timeout=15,
        )


@pytest.fixture
def context(local_dsn, monkeypatch):
    owner_id = next(OWNERS)
    # create_app은 전역 settings의 CORS 값을 읽는다. 검사용 origin만 바꾸고 DB는 격리 DSN을 넘긴다.
    monkeypatch.setattr(settings, "cors_origins", ["http://localhost:8089"])
    app = create_app(local_dsn)
    # 사용자 구분은 공통 X-User-Id 의존성을 그대로 두고, 검사에서는 소유자만 고정한다.
    app.dependency_overrides[get_current_user_id] = lambda: owner_id
    with psycopg.connect(local_dsn, row_factory=dict_row) as connection:
        story = connection.execute(
            """INSERT INTO stories(owner_id,title,world_description) VALUES (%s,%s,%s)
               RETURNING id,saved_at""",
            (owner_id, "가상 스토리", "수정하면 안 되는 세계관"),
        ).fetchone()
    with TestClient(app) as client:
        yield client, app, story, owner_id, local_dsn
