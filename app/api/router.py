"""전체 API 라우터 조립. 에픽 라우터를 여기서 등록한다."""

from fastapi import APIRouter

from app.features.conversation.messaging.router import router as messaging_router
from app.features.conversation.starter.router import router as conversation_router
from app.features.creation.router import router as creation_router

api_router = APIRouter()
api_router.include_router(creation_router)
api_router.include_router(conversation_router)
api_router.include_router(messaging_router)
