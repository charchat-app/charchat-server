"""환경 설정. 값은 환경변수 또는 .env에서 읽는다."""

import secrets
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# staging·production의 AUTH_JWT_SECRET 최소 길이(HS256 키 256비트).
JWT_SECRET_MIN_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "charchat-server"
    api_prefix: str = "/api/v1"
    database_url: str | None = None
    # pytest 전용 DB. 이름이 _test로 끝나야 하며 테스트가 스키마를 지우고 다시 만든다.
    test_database_url: str | None = None
    # 브라우저에서 API를 부를 수 있는 출처. 기본값은 Expo 웹 개발 서버.
    cors_origins: list[str] = ["http://localhost:8081"]
    # 업로드 이미지 저장 폴더. 호스팅 전까지 각자 로컬 디스크에 저장한다(CREATE-US-06).
    media_root: str = "media"
    # 연결이 끊긴 이미지 파일을 지우기 전 기다리는 시간(시간 단위). 문서상 미정이라 임시 기본값이다.
    image_cleanup_grace_hours: float = 24
    # CHAT-US-02 응답 생성. 호출 모델은 세션의 selected_model_id(models 행)다.
    # fake: 네트워크 없이 정해진 가짜 응답을 조금씩 보낸다(기본, 로컬·테스트용).
    # openai_compatible: provider가 openai인 모델을 {CHAT_LLM_BASE_URL}/chat/completions로
    # 스트리밍 호출한다. 키는 환경변수 CHAT_LLM_API_KEY로만 넣는다.
    chat_llm_provider: Literal["fake", "openai_compatible"] = "fake"
    chat_llm_base_url: str | None = "https://api.openai.com/v1"
    chat_llm_api_key: str | None = None
    # 새 세션에 고정할 기본 모델의 models.name. 해당 행이 없으면 세션 시작을 503으로 거절한다.
    chat_default_model_name: str = "루나"
    # 공급자 연결·첫 응답 대기 시간(초).
    chat_llm_timeout_seconds: float = 60
    # thread: 요청과 별도 스레드에서 생성(기본). inline: 요청 처리 안에서 끝까지 생성(테스트용).
    chat_generation_worker: Literal["thread", "inline"] = "thread"
    # 스트리밍 원문을 DB에 모아 쓰는 최소 간격(밀리초).
    chat_stream_flush_ms: int = 100
    # 이 시간(초) 동안 갱신이 없는 진행 중 생성은 서버 중단으로 보고 실패 처리한다.
    chat_generation_stale_seconds: float = 180
    # fake 공급자의 조각 사이 지연(밀리초)과 강제 실패(before: 내용 없이, after: 일부 후).
    chat_fake_chunk_delay_ms: int = 30
    chat_fake_fail_mode: Literal["none", "before", "after"] = "none"
    # BASE-03 로그인. production에서 아래 개발용 플래그가 하나라도 켜져 있으면 시작하지 않는다.
    app_env: Literal["local", "test", "staging", "production"] = "local"
    # 켜면 Authorization 헤더가 없을 때만 예전 개발용 X-User-Id 헤더로 사용자를 정한다.
    auth_dev_header_enabled: bool = False
    # 켜면 dev 제공자(POST /auth/tokens grant_type=dev)로 키만으로 로그인할 수 있다.
    auth_dev_login_enabled: bool = False
    # 액세스 토큰 수명(초)과 리프레시 토큰 수명(일). 리프레시는 회전할 때마다 다시 연장된다.
    auth_access_ttl_seconds: int = Field(default=1800, gt=0)
    auth_refresh_ttl_days: int = Field(default=60, gt=0)
    # 온보딩 필수 동의 버전(약관·개인정보). 만 14세 이상 자기 확인은 'self-v1'로 고정한다.
    auth_terms_version: str = Field(default="draft-v1", min_length=1)
    auth_privacy_version: str = Field(default="draft-v1", min_length=1)
    # 이메일·비밀번호 회원가입과 로그인(POST /auth/signup, grant_type=password).
    auth_password_login_enabled: bool = True
    # 액세스 토큰(JWT HS256) 서명 키. staging·production은 32바이트 이상이 아니면 시작하지 않는다.
    # local·test에서 비워 두면 프로세스 시작 때 임시 키를 만든다(재시작하면 이전 액세스 토큰 무효).
    auth_jwt_secret: SecretStr = SecretStr("")
    auth_jwt_issuer: str = Field(default="charchat", min_length=1)

    @model_validator(mode="after")
    def _ephemeral_jwt_secret_for_local(self) -> "Settings":
        if self.app_env in ("local", "test") and not self.auth_jwt_secret.get_secret_value():
            self.auth_jwt_secret = SecretStr(secrets.token_urlsafe(48))
        return self

    def assert_safe_for_startup(self) -> None:
        """production에서 개발용 인증 경로가 켜져 있거나, staging·production에서 JWT 서명 키가
        없거나 32바이트 미만이면 시작을 거부한다."""
        secret_bytes = len(self.auth_jwt_secret.get_secret_value().encode("utf-8"))
        if self.app_env in ("staging", "production") and secret_bytes < JWT_SECRET_MIN_BYTES:
            raise RuntimeError(
                f"APP_ENV={self.app_env}에서는 AUTH_JWT_SECRET을 "
                f"{JWT_SECRET_MIN_BYTES}바이트 이상으로 설정해야 합니다."
            )
        if self.app_env != "production":
            return
        enabled = [
            name
            for name, on in (
                ("AUTH_DEV_HEADER_ENABLED", self.auth_dev_header_enabled),
                ("AUTH_DEV_LOGIN_ENABLED", self.auth_dev_login_enabled),
            )
            if on
        ]
        if enabled:
            raise RuntimeError(
                f"APP_ENV=production에서는 개발용 인증 설정을 켤 수 없습니다: {', '.join(enabled)}"
            )


settings = Settings()
