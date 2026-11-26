"""BASE-03 내 계정 조회와 온보딩(pending → active).

필수 동의 3개(약관·개인정보·만 14세 이상)를 현재 버전으로 기록하고 기본 플레이 이름이 있으면
계정을 active로 바꾼다. 기본 플레이 이름은 기존 user_play_settings에 저장한다.
"""

from psycopg import Connection

from app.core.config import settings
from app.core.exceptions import AppError
from app.features.account.me import repository
from app.features.account.me.schemas import (
    AGE_CONSENT_VERSION,
    ConsentVersionsResponse,
    MeResponse,
    MeUpdateRequest,
    RequiredConsentsResponse,
)
from app.features.creation.common.names import has_surrounding_space

# 기본 플레이 이름 상한(임시 규칙 1~20자). 대화 시작은 이름+프로필 설명 1000자 한도로 다시 검사한다.
PLAY_NAME_MAX_LENGTH = 20
USER_PLACEHOLDER = "{{user}}"
FORBIDDEN_PLAY_NAME_CHARS = ("\x00", "\n", "\r")


class AccountNotFoundError(AppError):
    status = 404
    code = "ACCOUNT_NOT_FOUND"
    title = "계정을 찾을 수 없습니다"


class ConsentVersionMismatchError(AppError):
    status = 409
    code = "CONSENT_VERSION_MISMATCH"
    title = "동의한 약관 버전이 현재 버전과 다릅니다"


class PlayNameInvalidError(AppError):
    status = 422
    code = "PLAY_NAME_INVALID"
    title = "기본 플레이 이름을 사용할 수 없습니다"


def required_consents() -> dict[str, str]:
    return {
        "terms": settings.auth_terms_version,
        "privacy": settings.auth_privacy_version,
        "age_14_plus": AGE_CONSENT_VERSION,
    }


def get_me(conn: Connection, user_id: int) -> MeResponse:
    user = repository.get_user(conn, user_id)
    if user is None:
        raise AccountNotFoundError()
    latest = repository.latest_consents(conn, user_id)
    required = required_consents()
    return MeResponse(
        user_id=user["id"],
        status=user["status"],
        providers=repository.list_providers(conn, user_id),
        default_play_name=repository.get_default_play_name(conn, user_id),
        consents=ConsentVersionsResponse(**{kind: latest.get(kind) for kind in required}),
        required_consents=RequiredConsentsResponse(
            terms=required["terms"], privacy=required["privacy"]
        ),
        onboarding_required=user["status"] == "pending",
    )


def _validate_play_name(name: str) -> None:
    if (
        not name.strip()
        or has_surrounding_space(name)
        or name == USER_PLACEHOLDER
        or any(char in name for char in FORBIDDEN_PLAY_NAME_CHARS)
    ):
        raise PlayNameInvalidError(
            "기본 플레이 이름은 비울 수 없고 앞뒤 공백·줄바꿈 없이 "
            f"{PLAY_NAME_MAX_LENGTH}자 이하로 입력하세요."
        )
    if len(name) > PLAY_NAME_MAX_LENGTH:
        raise PlayNameInvalidError(f"플레이 이름은 {PLAY_NAME_MAX_LENGTH}자 이하로 입력하세요.")


def update_me(conn: Connection, user_id: int, request: MeUpdateRequest) -> MeResponse:
    required = required_consents()
    agreed = request.consents.model_dump(exclude_none=True) if request.consents else {}
    mismatched = sorted(kind for kind, version in agreed.items() if version != required[kind])
    if mismatched:
        raise ConsentVersionMismatchError(
            f"현재 버전으로 다시 동의하세요: {', '.join(mismatched)}",
            required_consents=required,
        )
    if request.default_play_name is not None:
        _validate_play_name(request.default_play_name)

    with conn.transaction():
        user = repository.get_user(conn, user_id, lock=True)
        if user is None:
            raise AccountNotFoundError()
        for kind, version in agreed.items():
            repository.add_consent(conn, user_id, kind, version)
        if request.default_play_name is not None:
            repository.upsert_default_play_name(conn, user_id, request.default_play_name)
        if (
            user["status"] == "pending"
            and repository.has_consents(conn, user_id, required)
            and repository.get_default_play_name(conn, user_id)
        ):
            repository.activate_user(conn, user_id)
    return get_me(conn, user_id)
