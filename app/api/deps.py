"""공통 의존성(인증, DB 커넥션)."""

from typing import Annotated

from fastapi import Depends, Header, Request
from psycopg import Connection

from app.core.common_safety import CommonSafetyGuard
from app.core.exceptions import UnauthenticatedError
from app.db.session import get_conn


def get_current_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> int:
    """임시 인증. 계정 설계 전까지 개발용 `X-User-Id` 헤더로 사용자를 구분한다.

    계정·인증 방식이 정해지면 이 함수만 교체한다. 운영 인증으로 쓰지 않는다.
    """
    if x_user_id is None or not x_user_id.isdigit() or int(x_user_id) <= 0:
        raise UnauthenticatedError("X-User-Id 헤더에 양의 정수 사용자 ID를 넣으세요.")
    return int(x_user_id)


CurrentUserId = Annotated[int, Depends(get_current_user_id)]
DbConn = Annotated[Connection, Depends(get_conn)]


def get_common_safety(request: Request) -> CommonSafetyGuard | None:
    """CREATE-US-08-AC15 주입된 공통 안전 판정기. 없으면 None(기존 동작)."""
    return getattr(request.app.state, "common_safety", None)


CommonSafetyDep = Annotated[CommonSafetyGuard | None, Depends(get_common_safety)]
