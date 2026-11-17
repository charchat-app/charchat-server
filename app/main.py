"""FastAPI 앱 진입점."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.db.session import close_pool, open_pool


def create_app(database_url: str | None = None) -> FastAPI:
    """`database_url`을 주면 그 DB를, 없으면 설정의 `DATABASE_URL`을 쓴다."""
    url = database_url or settings.database_url

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if url:
            open_pool(url)
        yield
        close_pool()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
