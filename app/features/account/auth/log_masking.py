"""BASE-03 로그 마스킹: 로그인 토큰·비밀번호·인가 코드가 서버 로그에 남지 않게 가린다.

- 액세스 토큰(JWT `eyJ….….…`, 이전 형식 `cca_…`)·리프레시 토큰(`ccr_…`) 원문
- `password` 키의 값(`"password": "…"`, `password=…` 등 요청 본문·폼 모양)
- 쿼리 문자열의 `code`, `state`, `login_code` 값(제공자 콜백 주소가 접근 로그에 찍힐 때)

uvicorn 접근 로그는 `record.args`(클라이언트, 메서드, 경로, …)로 형식을 만들므로 메시지와
각 인자를 따로 가린다.

로거 필터는 그 로거에서 직접 만든 기록에만 돌고 하위 로거(`app.features.*`, `uvicorn.*`)에서
올라온 기록에는 돌지 않는다. 그래서 LogRecord 팩토리를 감싸 어느 로거·핸들러로 가든 기록이
만들어질 때 가린다.
"""

import logging
import re

_TOKEN = re.compile(r"\b(cca_|ccr_)[A-Za-z0-9_-]+")
# 서명이 빈 JWT(alg none)도 가린다.
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")
_PASSWORD = re.compile(
    r"""(["']?password["']?\s*[:=]\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s&,;}]+)""",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(r"([?&](?:code|state|login_code)=)[^&#\s\"']*")
MASK = "***"


def mask_secrets(text: str) -> str:
    text = _JWT.sub(MASK, text)
    text = _TOKEN.sub(rf"\g<1>{MASK}", text)
    text = _PASSWORD.sub(rf"\g<1>{MASK}", text)
    return _QUERY_SECRET.sub(rf"\g<1>{MASK}", text)


def _mask_value(value):
    return mask_secrets(value) if isinstance(value, str) else value


def _mask_args(args):
    if isinstance(args, tuple):
        return tuple(_mask_value(arg) for arg in args)
    if isinstance(args, dict):
        return {key: _mask_value(arg) for key, arg in args.items()}
    return args


def mask_record(record: logging.LogRecord) -> logging.LogRecord:
    """메시지와 인자를 따로 가린다(uvicorn 접근 로그처럼 args를 그대로 쓰는 포매터용).

    `password=%s`처럼 자리표시자가 가려져 형식이 깨지면, 완성된 문장을 만든 뒤 가린다.
    """
    msg, args = record.msg, record.args
    masked_args = _mask_args(args)
    if isinstance(msg, str):
        masked_msg = mask_secrets(msg)
        if args and masked_msg != msg:
            try:
                masked_msg % masked_args
            except (TypeError, ValueError, KeyError):
                try:
                    record.msg, record.args = mask_secrets(msg % args), None
                    return record
                except (TypeError, ValueError, KeyError):
                    pass  # 원래도 형식이 틀린 호출이다. 아래처럼 가리기만 한다.
        record.msg = masked_msg
    record.args = masked_args
    return record


class SecretMaskingFilter(logging.Filter):
    """특정 핸들러에 직접 붙일 때 쓰는 필터. 기본 설치는 `install_log_masking`의 팩토리다."""

    def filter(self, record: logging.LogRecord) -> bool:
        mask_record(record)
        return True


def install_log_masking() -> None:
    """LogRecord 팩토리를 감싸 모든 로거의 기록을 가린다. 여러 번 불러도 한 번만 감싼다."""
    current = logging.getLogRecordFactory()
    if getattr(current, "_charchat_masking", False):
        return

    def masking_factory(*args, **kwargs) -> logging.LogRecord:
        return mask_record(current(*args, **kwargs))

    masking_factory._charchat_masking = True
    logging.setLogRecordFactory(masking_factory)


def is_log_masking_installed() -> bool:
    return getattr(logging.getLogRecordFactory(), "_charchat_masking", False)
