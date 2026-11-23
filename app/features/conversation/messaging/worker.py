"""CHAT-US-02 응답 생성 작업 실행기.

생성은 요청 트랜잭션이 커밋된 뒤 시작하고 HTTP 연결과 무관하게 끝까지 진행한다(AC13·14).
작업은 요청 커넥션을 쓰지 않고 DB 작업마다 풀에서 자기 커넥션을 빌린다. 스트리밍 동안 커넥션을
붙잡지 않아 동시 생성이 요청 처리용 풀을 막지 않게 한다.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from psycopg import Connection

from app.core.config import settings
from app.db.session import get_conn


@contextmanager
def connect() -> Iterator[Connection]:
    """요청과 별개인 autocommit 커넥션 하나."""
    yield from get_conn()


def submit(job: Callable[..., None], *args) -> None:
    """설정에 따라 별도 스레드(thread) 또는 현재 흐름 안(inline, 테스트용)에서 실행한다."""
    if settings.chat_generation_worker == "inline":
        job(*args)
        return
    thread = threading.Thread(target=job, args=args, name="chat-generation", daemon=True)
    thread.start()
