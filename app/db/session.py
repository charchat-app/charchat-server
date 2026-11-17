"""DB 커넥션 풀과 요청 단위 커넥션.

커넥션은 autocommit으로 빌려주고, 트랜잭션 경계는 서비스가 `conn.transaction()`으로 정한다.
"""

from collections.abc import Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.exceptions import AppError

_pool: ConnectionPool | None = None


class DatabaseNotConfiguredError(AppError):
    status = 503
    code = "DATABASE_NOT_CONFIGURED"
    title = "DB 연결이 설정되지 않았습니다"


def open_pool(database_url: str) -> None:
    """앱 시작 시 한 번 호출한다."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            database_url,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_conn() -> Iterator[Connection]:
    """요청마다 풀에서 커넥션 하나를 빌려준다."""
    if _pool is None:
        raise DatabaseNotConfiguredError("DATABASE_URL을 설정하세요.")
    with _pool.connection() as conn:
        yield conn
