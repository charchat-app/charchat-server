"""BASE-03 로그인·토큰 API 계약."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.features.account.me.schemas import MeResponse

ClientPlatform = Literal["ios", "android", "web"]


class ProviderResponse(BaseModel):
    provider: str
    # password: 이메일·비밀번호(email). direct: 서버에 바로 토큰을 요청(dev 전용).
    # redirect·native는 후속 소셜 로그인에서 쓴다.
    flows: list[Literal["password", "direct", "redirect", "native"]]


class ProvidersResponse(BaseModel):
    providers: list[ProviderResponse]


class DevTokenRequest(BaseModel):
    """AUTH_DEV_LOGIN_ENABLED일 때만 쓰는 개발용 로그인. 같은 키는 같은 계정이다."""

    model_config = ConfigDict(extra="forbid")

    grant_type: Literal["dev"]
    dev_user_key: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    client_platform: ClientPlatform


# 이메일·비밀번호 규칙(EMAIL_INVALID·PASSWORD_INVALID)은 서비스가 검사한다. 여기서는 터무니없이 긴
# 입력만 VALIDATION_ERROR로 막는다.
EmailInput = Annotated[StrictStr, Field(max_length=1024)]
PasswordInput = Annotated[StrictStr, Field(max_length=1024)]


class SignupRequest(BaseModel):
    """이메일·비밀번호 회원가입. 가입한 계정은 온보딩 전(pending)이다."""

    model_config = ConfigDict(extra="forbid")

    email: EmailInput
    password: PasswordInput
    client_platform: ClientPlatform


class PasswordTokenRequest(BaseModel):
    """이메일·비밀번호 로그인."""

    model_config = ConfigDict(extra="forbid")

    grant_type: Literal["password"]
    email: EmailInput
    password: PasswordInput
    client_platform: ClientPlatform


class RefreshTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_type: Literal["refresh_token"]
    refresh_token: Annotated[StrictStr, Field(min_length=1, max_length=512)]


TokenRequest = Annotated[
    PasswordTokenRequest | DevTokenRequest | RefreshTokenRequest,
    Field(discriminator="grant_type"),
]


class TokenResponse(BaseModel):
    # JWT(HS256). 앱은 문자열로만 다룬다.
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    # 남은 수명(초).
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    user: MeResponse
    is_new_user: bool
