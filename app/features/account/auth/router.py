"""BASE-03 로그인·회원가입·토큰 엔드포인트."""

from typing import Annotated

from fastapi import APIRouter, Body, Response

from app.api.deps import DbConn, LogoutAccount
from app.features.account.auth import service
from app.features.account.auth.schemas import (
    DevTokenRequest,
    PasswordTokenRequest,
    ProvidersResponse,
    SignupRequest,
    TokenRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["account:auth"])


@router.get("/providers")
def list_providers() -> ProvidersResponse:
    return service.list_providers()


@router.post("/signup", status_code=201)
def signup(request: SignupRequest, conn: DbConn) -> TokenResponse:
    return service.signup(conn, request)


@router.post("/tokens", status_code=201)
def create_tokens(request: Annotated[TokenRequest, Body()], conn: DbConn) -> TokenResponse:
    if isinstance(request, PasswordTokenRequest):
        return service.login_password(conn, request)
    if isinstance(request, DevTokenRequest):
        return service.login_dev(conn, request)
    return service.refresh_tokens(conn, request)


@router.delete("/sessions/current", status_code=204)
def delete_current_session(account: LogoutAccount, conn: DbConn) -> Response:
    # 만료된 액세스 토큰도 받는다(서명·클레임·살아 있는 세션은 그대로 확인).
    service.logout(conn, account)
    return Response(status_code=204)
