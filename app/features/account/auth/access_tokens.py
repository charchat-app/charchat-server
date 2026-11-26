"""BASE-03 액세스 토큰(JWT, HS256) 발급과 서명·클레임 검증.

클레임: iss(AUTH_JWT_ISSUER), sub(user_id), sid(auth_sessions.id), iat, exp(iat + TTL),
typ("access"), jti(발급마다 다른 값). 세션 폐기·계정 상태 확인(DB)은 service가 한다.
"""

import re
import secrets
import time
from typing import NamedTuple

import jwt

from app.core.config import settings

ALGORITHM = "HS256"
TOKEN_TYPE = "access"
REQUIRED_CLAIMS = ["exp", "iat", "sub", "sid", "iss"]
# 형식(헤더.본문.서명, 서명 비어 있지 않음)과 길이가 틀리면 디코딩하지 않고 거절한다.
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_MAX_TOKEN_LENGTH = 2048
_ID = re.compile(r"^[1-9][0-9]{0,18}$")


class AccessClaims(NamedTuple):
    user_id: int
    session_id: int
    # 서명·클레임은 맞지만 exp가 지났다.
    expired: bool


def _secret() -> str:
    return settings.auth_jwt_secret.get_secret_value()


def issue_access_token(user_id: int, session_id: int) -> str:
    now = int(time.time())
    claims = {
        "iss": settings.auth_jwt_issuer,
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": now,
        "exp": now + settings.auth_access_ttl_seconds,
        "typ": TOKEN_TYPE,
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(claims, _secret(), algorithm=ALGORITHM, headers={"typ": "JWT"})


def decode_access_token(token: str) -> AccessClaims:
    """서명·알고리즘·발급자·필수 클레임을 확인한다. 틀리면 jwt.InvalidTokenError.

    만료는 서명이 맞을 때만 `expired=True`로 알려 준다(폐기 확인을 만료보다 먼저 하기 위해).
    """
    if len(token) > _MAX_TOKEN_LENGTH or not _JWT_SHAPE.fullmatch(token):
        raise jwt.InvalidTokenError("malformed")
    options = {"require": REQUIRED_CLAIMS}
    kwargs = {"algorithms": [ALGORITHM], "issuer": settings.auth_jwt_issuer}
    try:
        claims = jwt.decode(token, _secret(), options=options, **kwargs)
        expired = False
    except jwt.ExpiredSignatureError:
        claims = jwt.decode(token, _secret(), options={**options, "verify_exp": False}, **kwargs)
        expired = True
    sub, sid = claims.get("sub"), claims.get("sid")
    if (
        claims.get("typ") != TOKEN_TYPE
        or not isinstance(sub, str)
        or not isinstance(sid, str)
        or not _ID.fullmatch(sub)
        or not _ID.fullmatch(sid)
    ):
        raise jwt.InvalidTokenError("claims")
    return AccessClaims(user_id=int(sub), session_id=int(sid), expired=expired)
