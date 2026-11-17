"""공통 도메인 예외와 RFC 9457(Problem Details) 응답 변환.

기능별 예외는 각 기능 service.py에 둔다.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    """애플리케이션 예외의 기준 클래스. 하위 클래스에서 status·code·title을 정한다."""

    status: int = 400
    code: str = "BAD_REQUEST"
    title: str = "요청을 처리할 수 없습니다"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)


class UnauthenticatedError(AppError):
    status = 401
    code = "UNAUTHENTICATED"
    title = "사용자를 확인할 수 없습니다"


def problem_response(status: int, code: str, title: str, detail: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
    }
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return problem_response(exc.status, exc.code, exc.title, exc.detail, **exc.extra)


async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return problem_response(
        422,
        "VALIDATION_ERROR",
        "요청 형식이 올바르지 않습니다",
        "요청 본문 또는 파라미터 형식을 확인하세요.",
        errors=errors,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
