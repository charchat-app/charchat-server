"""BASE-03 로그인·토큰 엔드포인트."""

from typing import Annotated

from fastapi import APIRouter, Body, Response

from app.api.deps import CurrentAccount, DbConn
from app.features.account.auth import service
from app.features.account.auth.schemas import (
    DevTokenRequest,
    ProvidersResponse,
    TokenRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["account:auth"])


@router.get("/providers")
def list_providers() -> ProvidersResponse:
    return service.list_providers()


@router.post("/tokens", status_code=201)
def create_tokens(request: Annotated[TokenRequest, Body()], conn: DbConn) -> TokenResponse:
    if isinstance(request, DevTokenRequest):
        return service.login_dev(conn, request)
    return service.refresh_tokens(conn, request)


@router.delete("/sessions/current", status_code=204)
def delete_current_session(account: CurrentAccount, conn: DbConn) -> Response:
    service.logout(conn, account)
    return Response(status_code=204)
