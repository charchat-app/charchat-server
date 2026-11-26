"""BASE-03 내 계정 조회·온보딩 API 계약."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

AGE_CONSENT_VERSION = "self-v1"
# 동의 버전 문자열. 서버의 현재 필수 버전과 정확히 같아야 기록한다(서비스에서 검사).
ConsentVersion = Annotated[StrictStr, Field(min_length=1, max_length=64)]


class ConsentVersionsResponse(BaseModel):
    terms: str | None
    privacy: str | None
    age_14_plus: str | None


class RequiredConsentsResponse(BaseModel):
    terms: str
    privacy: str
    age_14_plus: Literal["self-v1"] = AGE_CONSENT_VERSION


class MeResponse(BaseModel):
    user_id: int
    status: Literal["pending", "active"]
    # 연결된 로그인 수단(email·dev, 후속 kakao·google·apple). 응답에 이메일 주소는 넣지 않는다.
    providers: list[str]
    default_play_name: str | None
    # 종류별로 가장 최근에 동의한 버전. 없으면 null.
    consents: ConsentVersionsResponse
    required_consents: RequiredConsentsResponse
    onboarding_required: bool


class ConsentsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terms: ConsentVersion | None = None
    privacy: ConsentVersion | None = None
    age_14_plus: ConsentVersion | None = None


class MeUpdateRequest(BaseModel):
    """온보딩·내 정보 수정. 보낸 항목만 반영한다."""

    model_config = ConfigDict(extra="forbid")

    # 이름 규칙(1~20자·앞뒤 공백·줄바꿈·NUL·{{user}})은 서비스가 PLAY_NAME_INVALID로 거절한다.
    default_play_name: StrictStr | None = None
    consents: ConsentsUpdateRequest | None = None
