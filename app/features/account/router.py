"""BASE-03 계정 에픽 라우터. 하위 기능 라우터를 묶는다."""

from fastapi import APIRouter

from app.features.account.auth.router import router as auth_router
from app.features.account.me.router import router as me_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(me_router)
