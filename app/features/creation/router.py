"""EPIC-CREATE(캐릭터 생성) 라우터. 하위 기능 라우터를 묶는다."""

from fastapi import APIRouter

from app.features.creation.images.router import router as images_router
from app.features.creation.initial_state.router import router as initial_state_router
from app.features.creation.intro.router import router as intro_router
from app.features.creation.save_visibility.router import (
    router as save_visibility_router,
)
from app.features.creation.speech_style.router import router as speech_style_router
from app.features.creation.story_basic.router import router as story_basic_router
from app.features.creation.user_profile.router import router as user_profile_router

router = APIRouter(prefix="/creation")
router.include_router(story_basic_router)
router.include_router(speech_style_router)
router.include_router(intro_router)
router.include_router(initial_state_router)
router.include_router(user_profile_router)
router.include_router(images_router)
router.include_router(save_visibility_router)
