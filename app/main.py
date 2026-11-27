"""FastAPI 앱 진입점."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.common_safety import CommonSafetyGuard
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.db.session import close_pool, open_pool
from app.features.account.auth.log_masking import install_log_masking
from app.features.creation.category_classification.worker import Classifier
from app.features.creation.save_visibility.rating_review import RatingReviewCoordinator


def create_app(
    database_url: str | None = None,
    rating_review: RatingReviewCoordinator | None = None,
    common_safety: CommonSafetyGuard | None = None,
    *,
    category_classifier: Classifier | None = None,
) -> FastAPI:
    """`database_url`을 주면 그 DB를, 없으면 설정의 `DATABASE_URL`을 쓴다.

    `rating_review`(CREATE-US-08-AC02)는 주입할 때만 등급 검토를 넘긴다. 공급자·보관 정책이
    정해지기 전까지 기본 앱은 끈다. 조정기의 수명(종료)은 만든 쪽이 관리한다.

    `common_safety`(CREATE-US-08-AC15)를 주입하면 관람자 스토리 조회·대화 시작·세션 조회·공개
    태그 검색에서 공통 금지 판정을 적용한다. 판정기·롤아웃이 정해지기 전까지 기본 앱은 끈다.

    `category_classifier`(CREATE-US-09)를 주입할 때만 최초 등록 뒤 AI 카테고리 분류를 응답 뒤
    백그라운드로 실행한다. 기본 앱은 끈다. 등급 검토 조정기와는 서로 독립이다.
    """
    url = database_url or settings.database_url
    # BASE-03: staging·production에서 개발용 인증이 켜져 있으면 시작하지 않는다. 로그 토큰은 가린다.
    settings.assert_safe_for_startup()
    install_log_masking()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if url:
            open_pool(url)
        yield
        close_pool()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.category_classifier = category_classifier
    app.state.category_database_url = url
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.rating_review = rating_review
    app.state.common_safety = common_safety
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
