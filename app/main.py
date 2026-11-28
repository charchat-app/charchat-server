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
from app.features.creation.save_visibility import runtime as rating_runtime
from app.features.creation.save_visibility.rating_review import RatingReviewCoordinator


def create_app(
    database_url: str | None = None,
    rating_review: RatingReviewCoordinator | None = None,
    common_safety: CommonSafetyGuard | None = None,
    *,
    category_classifier: Classifier | None = None,
) -> FastAPI:
    """`database_url`을 주면 그 DB를, 없으면 설정의 `DATABASE_URL`을 쓴다.

    `rating_review`(CREATE-US-08-AC02)를 주입하면 그 조정기로 등급 검토를 넘기며, 수명(종료)은
    만든 쪽이 관리한다. 주입하지 않고 `RATING_REVIEW_ENABLED`를 켜면 앱이 Jev 평가기·DB 결과
    보관소로 조정기를 만들어 lifespan 시작 때 열고, 종료 때 대기열·평가 중 검토를 모두 끝낸 뒤
    (DB 풀보다 먼저) 닫는다(전체 대기 상한 없음, `rating_runtime.close_coordinator`). 기본은 꺼짐.
    조정기가 없을 때(꺼짐·종료 중) SAFE로 정해진 확정 저장은 적용 등급을 SAFE로 되돌린다.

    앱이 조정기를 직접 만들 때는 `DATABASE_URL`과 `JEV_OPENROUTER_API_KEY`가 있어야 하며, 없거나
    키 형식이 틀리면 DB에 연결하기 전에 시작을 거부한다(메시지에 값은 싣지 않는다).

    `common_safety`(CREATE-US-08-AC15)를 주입하면 관람자 스토리 조회·대화 시작·세션 조회·공개
    태그 검색에서 공통 금지 판정을 적용한다. 판정기·롤아웃이 정해지기 전까지 기본 앱은 끈다.

    `category_classifier`(CREATE-US-09)를 주입할 때만 최초 등록 뒤 AI 카테고리 분류를 응답 뒤
    백그라운드로 실행한다. 기본 앱은 끈다. 등급 검토 조정기와는 서로 독립이다.
    """
    url = database_url or settings.database_url
    # BASE-03: staging·production에서 개발용 인증이 켜져 있으면 시작하지 않는다. 로그 토큰은 가린다.
    settings.assert_safe_for_startup()
    install_log_masking()

    # CREATE-US-08: 주입된 의존성이 설정보다 우선한다. 주입이 없을 때만 설정으로 만든다.
    # 공통 안전 판정기(`common_safety`)는 설정으로 만들지 않고 주입할 때만 쓴다.
    own_review = settings.rating_review_enabled and rating_review is None
    transport = None
    if own_review:
        if not url:
            raise RuntimeError("RATING_REVIEW_ENABLED를 켜면 DATABASE_URL을 설정해야 합니다.")
        transport = rating_runtime.jev_transport(settings.jev_openrouter_api_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if own_review and app.state.rating_review is not None:
            # 같은 앱의 lifespan이 겹쳐 들어오면 실행 중인 조정기·DB 풀을 건드리지 않는다.
            raise RuntimeError("등급 검토 조정기가 이미 실행 중입니다(lifespan 중복 시작).")
        owned = None
        try:
            if url:
                open_pool(url)
            if own_review:
                owned = rating_runtime.rating_review_coordinator(transport, url)
                app.state.rating_review = owned
            yield
        finally:
            try:
                if owned is not None:
                    if app.state.rating_review is owned:
                        app.state.rating_review = None
                    await rating_runtime.close_coordinator(owned)
            finally:
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
