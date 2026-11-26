"""BASE-03 로그인·토큰 API 계약."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.features.account.me.schemas import MeResponse

ClientPlatform = Literal["ios", "android", "web"]


class ProviderResponse(BaseModel):
    provider: str
    # direct: 서버에 바로 토큰을 요청(dev 전용). redirect·native는 후속 제공자 로그인에서 쓴다.
    flows: list[Literal["direct", "redirect", "native"]]


class ProvidersResponse(BaseModel):
    providers: list[ProviderResponse]


class DevTokenRequest(BaseModel):
    """AUTH_DEV_LOGIN_ENABLED일 때만 쓰는 개발용 로그인. 같은 키는 같은 계정이다."""

    model_config = ConfigDict(extra="forbid")

    grant_type: Literal["dev"]
    dev_user_key: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    client_platform: ClientPlatform


class RefreshTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_type: Literal["refresh_token"]
    refresh_token: Annotated[StrictStr, Field(min_length=1, max_length=512)]


TokenRequest = Annotated[DevTokenRequest | RefreshTokenRequest, Field(discriminator="grant_type")]


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    # 남은 수명(초).
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    user: MeResponse
    is_new_user: bool
