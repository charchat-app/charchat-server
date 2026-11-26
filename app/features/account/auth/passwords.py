"""BASE-03 이메일·비밀번호 규칙(임시)과 BCrypt 해시.

임시 규칙(PM 확정 전):
- 이메일: 앞뒤 공백 제거 후 소문자. `local@domain.tld` 모양, 254자 이하.
- 비밀번호: 8~64자, UTF-8 72바이트 이하(bcrypt 한계, 잘라 쓰지 않고 거절), 영문자·숫자 각 1개 이상,
  NUL 금지. 앞뒤 공백은 허용하고 그대로 해시한다.

비밀번호 원문은 로그·예외 메시지에 넣지 않는다.
"""

import re
from functools import cache

import bcrypt

BCRYPT_ROUNDS = 12
EMAIL_MAX_LENGTH = 254
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 64
# bcrypt는 72바이트 뒤를 무시한다. 넘으면 잘라 쓰지 않고 거절한다.
PASSWORD_MAX_BYTES = 72
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
# 없는 이메일·쓸 수 없는 비밀번호일 때도 bcrypt 검사를 한 번 하기 위한 비교 대상.
_DUMMY_PASSWORD = b"charchat-dummy-password-0"


def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def is_valid_email(email: str) -> bool:
    """normalize_email을 거친 값을 검사한다."""
    return len(email) <= EMAIL_MAX_LENGTH and _EMAIL.fullmatch(email) is not None


def password_problems(password: str) -> list[str]:
    """맞지 않는 규칙의 안내 문구(한국어). 비어 있으면 규칙에 맞다."""
    problems = []
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        problems.append(f"비밀번호는 {PASSWORD_MIN_LENGTH}~{PASSWORD_MAX_LENGTH}자로 입력하세요.")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        problems.append(
            f"비밀번호가 너무 깁니다. UTF-8 기준 {PASSWORD_MAX_BYTES}바이트 이하여야 합니다."
        )
    if not _LETTER.search(password):
        problems.append("영문자를 1개 이상 넣으세요.")
    if not _DIGIT.search(password):
        problems.append("숫자를 1개 이상 넣으세요.")
    if "\x00" in password:
        problems.append("NUL 문자는 쓸 수 없습니다.")
    return problems


def hash_password(password: str) -> str:
    """규칙을 통과한 비밀번호의 bcrypt 해시(`$2b$12$…`)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode(
        "ascii"
    )


@cache
def _dummy_hash() -> bytes:
    return bcrypt.hashpw(_DUMMY_PASSWORD, bcrypt.gensalt(rounds=BCRYPT_ROUNDS))


def check_password(password: str, password_hash: str | None) -> bool:
    """비밀번호가 해시와 맞으면 True.

    해시가 없거나(없는 이메일) 비밀번호를 bcrypt에 넣을 수 없어도(72바이트 초과·NUL) 더미 해시로
    bcrypt 검사를 한 번 해서 응답 시간 차이를 줄인다.
    """
    candidate = password.encode("utf-8")
    usable = len(candidate) <= PASSWORD_MAX_BYTES and b"\x00" not in candidate
    if not usable:
        candidate = _DUMMY_PASSWORD + b"!"
    target = password_hash.encode("ascii") if password_hash is not None else _dummy_hash()
    matched = bcrypt.checkpw(candidate, target)
    return matched and usable and password_hash is not None
