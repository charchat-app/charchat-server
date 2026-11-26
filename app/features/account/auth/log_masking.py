"""BASE-03 로그 마스킹: 로그인 토큰과 인가 코드가 서버 로그에 남지 않게 가린다.

- `cca_…`(액세스)·`ccr_…`(리프레시) 토큰 원문
- 쿼리 문자열의 `code`, `state`, `login_code` 값(제공자 콜백 주소가 접근 로그에 찍힐 때)

uvicorn 접근 로그는 `record.args`(클라이언트, 메서드, 경로, …)로 형식을 만들므로 메시지와
각 인자를 따로 가린다.
"""

import logging
import re

_TOKEN = re.compile(r"\b(cca_|ccr_)[A-Za-z0-9_-]+")
_QUERY_SECRET = re.compile(r"([?&](?:code|state|login_code)=)[^&#\s\"']*")
MASK = "***"
# 필터를 붙일 로거. uvicorn.access는 요청 줄(쿼리 포함), uvicorn.error는 서버 오류 로그다.
MASKED_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn", "fastapi", "app")


def mask_secrets(text: str) -> str:
    return _QUERY_SECRET.sub(rf"\g<1>{MASK}", _TOKEN.sub(rf"\g<1>{MASK}", text))


def _mask_value(value):
    return mask_secrets(value) if isinstance(value, str) else value


class SecretMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_mask_value(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _mask_value(arg) for key, arg in record.args.items()}
        return True


def install_log_masking() -> None:
    """여러 번 불러도 로거마다 필터는 하나만 붙는다."""
    for name in MASKED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, SecretMaskingFilter) for f in logger.filters):
            logger.addFilter(SecretMaskingFilter())
