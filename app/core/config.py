"""환경 설정. 값은 환경변수 또는 .env에서 읽는다."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
