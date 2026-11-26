"""BASE-03 로그인: 이메일·비밀번호 가입·로그인, JWT 액세스 토큰, 리프레시 토큰 회전·폐기.

- 액세스 토큰은 JWT(HS256, `access_tokens`)라 저장하지 않는다. 서명을 확인한 뒤 sid로
  auth_sessions·users를 한 번 읽어 로그아웃(폐기)을 바로 반영하고 pending/active를 최신으로 본다.
- 리프레시 토큰 `ccr_…`(`secrets.token_urlsafe(32)`)은 DB에 sha256만 둔다. 리프레시할 때마다
  리프레시 토큰을 새로 발급하고 새 JWT를 준다. 이전 JWT는 세션이 살아 있으면 만료까지 쓸 수 있다.
  직전 리프레시 토큰이 회전 후 `REFRESH_REUSE_GRACE_SECONDS` 안에 다시 오면 409(동시 요청으로 보고
  폐기하지 않음), 그 뒤에 오면 탈취 가능성으로 보고 세션을 폐기한 뒤 401을 낸다.
- 비밀번호는 bcrypt(cost 12) 해시만 저장한다(`passwords`).
- 토큰·비밀번호 원문은 로그·예외 메시지에 넣지 않는다.
"""

import hashlib
import re
import secrets
from typing import NamedTuple

import jwt
from psycopg import Connection
from psycopg.errors import CheckViolation

from app.core.config import settings
from app.core.exceptions import AppError, UnauthenticatedError
from app.features.account.auth import access_tokens, passwords, repository
from app.features.account.auth.schemas import (
    DevTokenRequest,
    PasswordTokenRequest,
    ProviderResponse,
    ProvidersResponse,
    RefreshTokenRequest,
    SignupRequest,
    TokenResponse,
)
from app.features.account.me import service as me_service

REFRESH_PREFIX = "ccr_"
REFRESH_REUSE_GRACE_SECONDS = 10
# 접두어 + token_urlsafe(32)(43자). 형식이 다르면 DB를 조회하지 않고 거절한다.
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


class PasswordLoginDisabledError(AppError):
    status = 404
    code = "AUTH_PASSWORD_LOGIN_DISABLED"
    title = "이메일 로그인이 꺼져 있습니다"


class InvalidCredentialsError(AppError):
    status = 401
    code = "AUTH_INVALID_CREDENTIALS"
    title = "이메일 또는 비밀번호가 맞지 않습니다"


class EmailInvalidError(AppError):
    status = 422
    code = "EMAIL_INVALID"
    title = "이메일 형식이 올바르지 않습니다"


class PasswordInvalidError(AppError):
    status = 422
    code = "PASSWORD_INVALID"
    title = "비밀번호 규칙에 맞지 않습니다"


class EmailAlreadyRegisteredError(AppError):
    status = 409
    code = "EMAIL_ALREADY_REGISTERED"
    title = "이미 가입한 이메일입니다"


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _new_refresh_token() -> str:
    return REFRESH_PREFIX + secrets.token_urlsafe(32)


def _parse_bearer(authorization: str) -> str:
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AuthTokenInvalidError(
            "Authorization 헤더는 'Bearer <액세스 토큰>' 형식이어야 합니다."
        )
    return token


def authenticate_access_token(conn: Connection, authorization: str) -> Account:
    """Authorization 헤더의 JWT로 계정을 찾는다.

    서명·형식·발급자가 틀리거나 세션이 폐기·없음·sub 불일치·탈퇴 계정이면 INVALID.
    서명이 맞고 세션도 살아 있는데 exp만 지났으면 EXPIRED(앱이 리프레시한다).
    """
    token = _parse_bearer(authorization)
    try:
        claims = access_tokens.decode_access_token(token)
    except jwt.InvalidTokenError:
        raise AuthTokenInvalidError() from None
    row = repository.find_session(conn, claims.session_id)
    if (
        row is None
        or row["revoked"]
        or row["user_id"] != claims.user_id
        or row["status"] == "deleted"
    ):
        raise AuthTokenInvalidError()
    if claims.expired:
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
    if settings.auth_password_login_enabled:
        providers.append(ProviderResponse(provider="email", flows=["password"]))
    if settings.auth_dev_login_enabled:
        providers.append(ProviderResponse(provider="dev", flows=["direct"]))
    return ProvidersResponse(providers=providers)


def _create_session(conn: Connection, user_id: int, client_platform: str, refresh: str) -> int:
    return repository.create_session(
        conn, user_id, client_platform, hash_token(refresh), settings.auth_refresh_ttl_days
    )


def _issue(
    conn: Connection, user_id: int, session_id: int, refresh: str, is_new_user: bool
) -> TokenResponse:
    return TokenResponse(
        access_token=access_tokens.issue_access_token(user_id, session_id),
        expires_in=settings.auth_access_ttl_seconds,
        refresh_token=refresh,
        refresh_expires_in=settings.auth_refresh_ttl_days * 86400,
        user=me_service.get_me(conn, user_id),
        is_new_user=is_new_user,
    )


def _require_password_login() -> None:
    if not settings.auth_password_login_enabled:
        raise PasswordLoginDisabledError()


def signup(conn: Connection, request: SignupRequest) -> TokenResponse:
    """이메일·비밀번호 회원가입. pending 계정과 로그인 세션을 만든다."""
    _require_password_login()
    email = passwords.normalize_email(request.email)
    if not passwords.is_valid_email(email):
        raise EmailInvalidError(f"이메일 형식을 확인하세요({passwords.EMAIL_MAX_LENGTH}자 이하).")
    problems = passwords.password_problems(request.password)
    if problems:
        raise PasswordInvalidError(" ".join(problems))
    password_hash = passwords.hash_password(request.password)
    refresh = _new_refresh_token()
    try:
        with conn.transaction():
            user_id = repository.create_pending_user(conn)
            # 같은 이메일의 동시 가입은 유일 제약에서 하나만 남는다. 진 쪽은 계정 생성까지 되돌린다.
            if not repository.create_password_credential(conn, user_id, email, password_hash):
                raise EmailAlreadyRegisteredError("로그인하거나 다른 이메일을 쓰세요.")
            session_id = _create_session(conn, user_id, request.client_platform, refresh)
    except CheckViolation:
        # 파이썬과 DB의 소문자 규칙이 다른 문자 등. 저장하지 않고 형식 오류로 돌려준다.
        raise EmailInvalidError("이메일 형식을 확인하세요.") from None
    return _issue(conn, user_id, session_id, refresh, True)


def login_password(conn: Connection, request: PasswordTokenRequest) -> TokenResponse:
    """이메일·비밀번호 로그인. 없는 이메일과 틀린 비밀번호는 같은 401이다."""
    _require_password_login()
    email = passwords.normalize_email(request.email)
    row = None
    if passwords.is_valid_email(email):
        row = repository.find_password_credential(conn, email)
    stored = row["password_hash"] if row is not None and row["status"] != "deleted" else None
    # 없는 이메일도 더미 해시로 bcrypt 검사를 한 번 한다(응답 시간 차이 줄이기).
    if not passwords.check_password(request.password, stored):
        raise InvalidCredentialsError()
    refresh = _new_refresh_token()
    with conn.transaction():
        session_id = _create_session(conn, row["user_id"], request.client_platform, refresh)
    return _issue(conn, row["user_id"], session_id, refresh, False)


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
    refresh = _new_refresh_token()
    with conn.transaction():
        user_id, is_new_user = _find_or_create_user(conn, "dev", request.dev_user_key)
        session_id = _create_session(conn, user_id, request.client_platform, refresh)
    return _issue(conn, user_id, session_id, refresh, is_new_user)


def refresh_tokens(conn: Connection, request: RefreshTokenRequest) -> TokenResponse:
    """리프레시 토큰 회전과 새 JWT 발급. 실패 결과(폐기)는 커밋한 뒤 예외를 낸다."""
    if not _REFRESH_TOKEN.fullmatch(request.refresh_token):
        raise RefreshInvalidError()
    refresh = _new_refresh_token()
    error: AppError | None = None
    user_id = session_id = 0
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
            user_id, session_id = row["user_id"], row["id"]
            repository.rotate_session(
                conn, session_id, hash_token(refresh), settings.auth_refresh_ttl_days
            )
    if error is not None:
        raise error
    return _issue(conn, user_id, session_id, refresh, False)


def logout(conn: Connection, account: Account) -> None:
    """현재 로그인 세션을 폐기한다. 개발용 헤더 사용자는 폐기할 세션이 없다."""
    if account.session_id is None:
        raise UnauthenticatedError("로그아웃할 로그인 세션이 없습니다. Bearer 토큰을 보내세요.")
    repository.revoke_session(conn, account.session_id, "logout")
