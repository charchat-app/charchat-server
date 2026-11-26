"""BASE-03 로그인 세션: 불투명 토큰 발급·확인·회전·폐기.

- 액세스 토큰 `cca_…`, 리프레시 토큰 `ccr_…`(`secrets.token_urlsafe(32)`). DB에는 sha256만 둔다.
- 리프레시할 때마다 두 토큰을 모두 새로 발급한다. 직전 리프레시 토큰이 회전 후
  `REFRESH_REUSE_GRACE_SECONDS` 안에 다시 오면 409(동시 요청으로 보고 폐기하지 않음),
  그 뒤에 오면 탈취 가능성으로 보고 세션을 폐기한 뒤 401을 낸다.
- 토큰 원문은 로그·예외 메시지에 넣지 않는다.
"""

import hashlib
import re
import secrets
from typing import NamedTuple

from psycopg import Connection

from app.core.config import settings
from app.core.exceptions import AppError, UnauthenticatedError
from app.features.account.auth import repository
from app.features.account.auth.schemas import (
    DevTokenRequest,
    ProviderResponse,
    ProvidersResponse,
    RefreshTokenRequest,
    TokenResponse,
)
from app.features.account.me import service as me_service

ACCESS_PREFIX = "cca_"
REFRESH_PREFIX = "ccr_"
REFRESH_REUSE_GRACE_SECONDS = 10
# 접두어 + token_urlsafe(32)(43자). 형식이 다르면 DB를 조회하지 않고 거절한다.
_ACCESS_TOKEN = re.compile(r"^cca_[A-Za-z0-9_-]{43}$")
_REFRESH_TOKEN = re.compile(r"^ccr_[A-Za-z0-9_-]{43}$")


class Account(NamedTuple):
    """요청을 보낸 계정. session_id가 None이면 개발용 X-User-Id 헤더로 정한 사용자다."""

    user_id: int
    status: str
    session_id: int | None


class AuthTokenExpiredError(AppError):
    status = 401
    code = "AUTH_TOKEN_EXPIRED"
    title = "로그인이 만료되었습니다"


class AuthTokenInvalidError(AppError):
    status = 401
    code = "AUTH_TOKEN_INVALID"
    title = "로그인 정보가 올바르지 않습니다"


class OnboardingRequiredError(AppError):
    status = 403
    code = "ONBOARDING_REQUIRED"
    title = "가입 절차를 먼저 마쳐야 합니다"


class RefreshInvalidError(AppError):
    status = 401
    code = "AUTH_REFRESH_INVALID"
    title = "다시 로그인해야 합니다"


class RefreshConflictError(AppError):
    status = 409
    code = "AUTH_REFRESH_CONFLICT"
    title = "이미 갱신된 로그인입니다"


class DevLoginDisabledError(AppError):
    status = 404
    code = "AUTH_DEV_LOGIN_DISABLED"
    title = "개발용 로그인이 꺼져 있습니다"


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _parse_bearer(authorization: str) -> str:
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not _ACCESS_TOKEN.fullmatch(token):
        raise AuthTokenInvalidError(
            "Authorization 헤더는 'Bearer <액세스 토큰>' 형식이어야 합니다."
        )
    return token


def authenticate_access_token(conn: Connection, authorization: str) -> Account:
    """Authorization 헤더의 액세스 토큰으로 계정을 찾는다. 폐기·미등록은 INVALID, 만료는 EXPIRED."""
    token = _parse_bearer(authorization)
    row = repository.find_session_by_access(conn, hash_token(token))
    if row is None or row["revoked"] or row["status"] == "deleted":
        raise AuthTokenInvalidError()
    if row["expired"]:
        raise AuthTokenExpiredError("액세스 토큰을 리프레시 토큰으로 갱신하세요.")
    if row["stale"]:
        # 쓰기를 줄이려고 1분 이상 지났을 때만 갱신한다.
        repository.touch_session(conn, row["id"])
    return Account(user_id=row["user_id"], status=row["status"], session_id=row["id"])


def dev_header_account(x_user_id: str) -> Account:
    """개발용 X-User-Id 헤더. 형식이 틀리면 401. users 행은 확인하지 않는다(기존 데이터 호환)."""
    if not x_user_id.isdigit() or int(x_user_id) <= 0:
        raise UnauthenticatedError("X-User-Id 헤더에 양의 정수 사용자 ID를 넣으세요.")
    return Account(user_id=int(x_user_id), status="active", session_id=None)


def require_active(account: Account) -> int:
    if account.status != "active":
        raise OnboardingRequiredError("약관 동의와 기본 플레이 이름 입력을 먼저 마치세요.")
    return account.user_id


def list_providers() -> ProvidersResponse:
    providers = []
    if settings.auth_dev_login_enabled:
        providers.append(ProviderResponse(provider="dev", flows=["direct"]))
    return ProvidersResponse(providers=providers)


def _issue(conn: Connection, user_id: int, is_new_user: bool, tokens: tuple[str, str]):
    access, refresh = tokens
    return TokenResponse(
        access_token=access,
        expires_in=settings.auth_access_ttl_seconds,
        refresh_token=refresh,
        refresh_expires_in=settings.auth_refresh_ttl_days * 86400,
        user=me_service.get_me(conn, user_id),
        is_new_user=is_new_user,
    )


def _find_or_create_user(conn: Connection, provider: str, subject: str) -> tuple[int, bool]:
    """(provider, subject)의 계정을 찾고 없으면 pending 계정을 만든다. (user_id, 새 계정 여부)."""
    identity = repository.find_identity(conn, provider, subject)
    if identity is None:
        user_id = repository.create_pending_user(conn)
        if repository.create_identity(conn, user_id, provider, subject):
            return user_id, True
        # 같은 계정의 동시 첫 로그인이 먼저 만들었다. 방금 만든 계정은 버리고 그 계정을 쓴다.
        repository.delete_user(conn, user_id)
        identity = repository.find_identity(conn, provider, subject)
        if identity is None:
            raise AuthTokenInvalidError()
    if identity["status"] == "deleted":
        raise AuthTokenInvalidError("탈퇴한 계정입니다.")
    repository.touch_identity(conn, identity["id"])
    return identity["user_id"], False


def login_dev(conn: Connection, request: DevTokenRequest) -> TokenResponse:
    """dev 제공자 로그인. 처음 보는 키면 pending 계정을 새로 만든다."""
    if not settings.auth_dev_login_enabled:
        raise DevLoginDisabledError()
    access, refresh = _new_token(ACCESS_PREFIX), _new_token(REFRESH_PREFIX)
    with conn.transaction():
        user_id, is_new_user = _find_or_create_user(conn, "dev", request.dev_user_key)
        repository.create_session(
            conn,
            user_id,
            request.client_platform,
            hash_token(access),
            hash_token(refresh),
            settings.auth_access_ttl_seconds,
            settings.auth_refresh_ttl_days,
        )
    return _issue(conn, user_id, is_new_user, (access, refresh))


def refresh_tokens(conn: Connection, request: RefreshTokenRequest) -> TokenResponse:
    """리프레시 토큰 회전. 실패 결과(폐기)는 커밋한 뒤 예외를 낸다."""
    if not _REFRESH_TOKEN.fullmatch(request.refresh_token):
        raise RefreshInvalidError()
    access, refresh = _new_token(ACCESS_PREFIX), _new_token(REFRESH_PREFIX)
    error: AppError | None = None
    user_id = 0
    with conn.transaction():
        row = repository.lock_session_by_refresh(
            conn, hash_token(request.refresh_token), REFRESH_REUSE_GRACE_SECONDS
        )
        if row is None or row["revoked"] or row["status"] == "deleted":
            error = RefreshInvalidError()
        elif not row["is_current"]:
            if row["within_grace"]:
                error = RefreshConflictError("방금 갱신된 토큰을 사용하세요.")
            else:
                repository.revoke_session(conn, row["id"], "refresh_reuse")
                error = RefreshInvalidError()
        elif row["refresh_expired"]:
            repository.revoke_session(conn, row["id"], "expired")
            error = RefreshInvalidError()
        else:
            user_id = row["user_id"]
            repository.rotate_session(
                conn,
                row["id"],
                hash_token(access),
                hash_token(refresh),
                settings.auth_access_ttl_seconds,
                settings.auth_refresh_ttl_days,
            )
    if error is not None:
        raise error
    return _issue(conn, user_id, False, (access, refresh))


def logout(conn: Connection, account: Account) -> None:
    """현재 로그인 세션을 폐기한다. 개발용 헤더 사용자는 폐기할 세션이 없다."""
    if account.session_id is None:
        raise UnauthenticatedError("로그아웃할 로그인 세션이 없습니다. Bearer 토큰을 보내세요.")
    repository.revoke_session(conn, account.session_id, "logout")
