"""생성 기능 공통 오류 생성. 코드별 하위 클래스를 만들지 않고 상태·코드·안내를 바로 지정한다.

app.core.exceptions.AppError(Problem Details)를 그대로 쓰며, 응답 형식은 공통 핸들러가 정한다.
"""

from typing import Any

from app.core.exceptions import AppError


def app_error(status: int, code: str, detail: str, **extra: Any) -> AppError:
    """예: raise app_error(422, "INTRO_TOO_LONG", "인트로 본문은 2000자 이내로 작성해 주세요.")"""
    error = AppError(detail, **extra)
    error.status = status
    error.code = code
    return error
