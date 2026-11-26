"""BASE-03 내 계정 조회·온보딩 엔드포인트. pending 계정도 쓸 수 있다."""

from fastapi import APIRouter

from app.api.deps import CurrentAccount, DbConn
from app.features.account.me import service
from app.features.account.me.schemas import MeResponse, MeUpdateRequest

router = APIRouter(tags=["account:me"])


@router.get("/me")
def get_me(account: CurrentAccount, conn: DbConn) -> MeResponse:
    return service.get_me(conn, account.user_id)


@router.patch("/me")
def update_me(request: MeUpdateRequest, account: CurrentAccount, conn: DbConn) -> MeResponse:
    return service.update_me(conn, account.user_id, request)
