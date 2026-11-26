"""공통 의존성(인증, DB 커넥션).

인증 규칙(BASE-03):
1. Authorization 헤더가 있으면 반드시 `Bearer <JWT>` 액세스 토큰으로만 사용자를 정한다.
   이때 X-User-Id는 보지 않는다(헤더 조작으로 남의 계정에 들어갈 수 없게).
2. Authorization이 없고 `AUTH_DEV_HEADER_ENABLED`가 켜져 있을 때만 개발용 X-User-Id를 받는다.
3. 그 밖에는 401 UNAUTHENTICATED.

토큰 확인은 요청 커넥션(DbConn)을 쓰지 않고 풀에서 잠깐 빌렸다가 바로 돌려준다. 요청 커넥션을
받지 않는 롱 폴링(CHAT-US-02 생성 조회)이 기다리는 동안 커넥션을 붙잡지 않게 하기 위해서다.
"""

from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Header
from psycopg import Connection

from app.core.config import settings
from app.core.exceptions import UnauthenticatedError
from app.db.session import get_conn
from app.features.account.auth.service import (
    Account,
    authenticate_access_token,
    dev_header_account,
    require_active,
)

DbConn = Annotated[Connection, Depends(get_conn)]
AuthorizationHeader = Annotated[str | None, Header(alias="Authorization")]
DevUserHeader = Annotated[str | None, Header(alias="X-User-Id")]
_borrow_conn = contextmanager(get_conn)


def _resolve_account(
    authorization: str | None, x_user_id: str | None, *, allow_expired: bool = False
) -> Account | None:
    if authorization is not None:
        with _borrow_conn() as conn:
            return authenticate_access_token(conn, authorization, allow_expired=allow_expired)
    if settings.auth_dev_header_enabled and x_user_id is not None:
        return dev_header_account(x_user_id)
    return None


def get_current_account(
    authorization: AuthorizationHeader = None,
    x_user_id: DevUserHeader = None,
) -> Account:
    """로그인한 계정. pending(온보딩 전) 계정도 통과한다. /me 계열 전용."""
    account = _resolve_account(authorization, x_user_id)
    if account is None:
        raise UnauthenticatedError("Authorization: Bearer 액세스 토큰을 보내세요.")
    return account


CurrentAccount = Annotated[Account, Depends(get_current_account)]


def get_logout_account(
    authorization: AuthorizationHeader = None,
    x_user_id: DevUserHeader = None,
) -> Account:
    """로그아웃(DELETE /auth/sessions/current) 전용. 서명·발급자·클레임이 맞고 세션이 살아 있으면
    exp가 지난 액세스 토큰도 받는다(앱을 오래 켜 둔 뒤 로그아웃해도 서버 세션을 끊기 위해)."""
    account = _resolve_account(authorization, x_user_id, allow_expired=True)
    if account is None:
        raise UnauthenticatedError("Authorization: Bearer <액세스 토큰>을 보내세요.")
    return account


LogoutAccount = Annotated[Account, Depends(get_logout_account)]


def get_current_user_id(account: CurrentAccount) -> int:
    """active 계정의 사용자 ID. pending 계정은 403 ONBOARDING_REQUIRED.

    기존 엔드포인트는 `CurrentUserId` 이름 그대로 이 함수를 쓴다.
    """
    return require_active(account)


def get_optional_user_id(
    authorization: AuthorizationHeader = None,
    x_user_id: DevUserHeader = None,
) -> int | None:
    """로그인 없이도 쓸 수 있는 조회(공개 이미지 등)의 선택적 사용자.

    자격을 보내지 않으면 None. 보낸 자격이 틀리면 공통 인증과 같게 401(pending은 403)로 거절한다.
    """
    account = _resolve_account(authorization, x_user_id)
    return None if account is None else require_active(account)


CurrentUserId = Annotated[int, Depends(get_current_user_id)]
