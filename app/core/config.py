"""환경 설정. 값은 환경변수 또는 .env에서 읽는다."""

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


settings = Settings()
