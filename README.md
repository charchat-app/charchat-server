# charchat-server

캐릭터챗 백엔드 (FastAPI). 요구사항 원본은 `charchat-plan/kickoff`를 따른다.

## 구조 원칙

- **최상위 = 에픽**: `app/features/<epic>/` 아래에 에픽별 패키지를 둔다. 에픽은 기능 분류이며 담당 영역이 아니다.
- **하위 = 스토리(기능)**: 스토리 1개가 폴더 1개다. 기능 폴더에는 `router.py`만 두고, 로직·스키마·DB 파일은 실제로 필요할 때 추가한다.
- **공통 = `common/`**: 여러 스토리가 함께 쓰는 모델·로직(스토리 루트, 공통 이미지 등)은 에픽 안의 `common/`에 둔다.
- **엔드포인트 우선**: 현재는 골격만 두었다. 엔드포인트는 기능 폴더의 `router.py`에 추가한다.
- **계층(추가 시)**: `router.py`(HTTP) → `service.py`(로직) → `repository.py`(DB). 라우터가 DB를 직접 호출하지 않는다.

```
app/
├── main.py                  # FastAPI 앱 생성·라우터 등록
├── api/
│   ├── router.py            # 전체 라우터 조립 (에픽 라우터 등록)
│   └── deps.py              # 공통 의존성
├── core/
│   ├── config.py            # 환경설정 (.env)
│   └── exceptions.py        # 공통 예외
├── db/                      # DB 연결·세션·모델 베이스 (DB 확정 후 구현)
└── features/
    └── creation/            # EPIC-CREATE — 캐릭터 생성
        ├── router.py        # 에픽 라우터 (하위 기능 묶음, prefix=/creation)
        ├── common/          # 여러 스토리가 함께 쓰는 모델·로직
        ├── story_basic/     # CREATE-US-01 스토리 기본 정보·세계관·캐릭터 설정
        ├── speech_style/    # CREATE-US-02 말투와 서술 방식 설정
        ├── intro/           # CREATE-US-03 첫 만남 인트로 설정
        ├── initial_state/   # CREATE-US-04 초기 배경 설정
        ├── user_profile/    # CREATE-US-05 크리에이터 기본 유저 프로필 설정
        ├── images/          # CREATE-US-06 이미지 등록·관리
        └── save_visibility/ # CREATE-US-07 설정 저장·공개 범위·재조회·수정
            ├── __init__.py
            └── router.py

tests/
├── conftest.py
└── features/creation/<feature>/   # app/features 와 같은 구조로 테스트
```

## 스토리 매핑 규칙

| 구분 | 규칙 |
| --- | --- |
| 폴더명 | 스토리 내용을 영문 snake_case로 줄인 이름 (ID 대신 의미 이름) |
| 모듈 docstring | 첫 줄에 스토리 ID(예: `CREATE-US-01`)를 적어 요구사항과 연결 |
| 이후 에픽 | `conversation`(CHAT-US-01~12), `extras`(EXTRA-US-01~05)도 같은 방식으로 추가 |

## 실행

사전 준비: Docker Desktop 실행 (Windows는 WSL2 백엔드 사용).

**macOS / Linux**
```bash
cp .env.example .env              # 값 수정 (.env는 git에 올라가지 않음)
docker compose up -d              # Postgres 기동
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
pytest
```

**Windows (PowerShell)**
```powershell
Copy-Item .env.example .env       # 값 수정 (.env는 git에 올라가지 않음)
docker compose up -d              # Postgres 기동
py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
pytest
```

### Postgres (docker compose)

- 이미지: `postgres:17`, 로컬 포트는 `127.0.0.1:${POSTGRES_PORT}`에만 연다.
- 데이터는 Docker가 관리하는 named volume `charchat_pgdata`에 저장된다. 프로젝트 폴더에는 아무것도 쌓이지 않는다.
- 접속 정보와 비밀번호는 `.env`에서만 읽는다. `.env`는 `.gitignore` 대상이다.
- 중지: `docker compose down` (데이터 유지). 데이터까지 삭제: `docker compose down -v`.
- 비밀번호를 바꿔도 기존 볼륨의 DB 비밀번호는 바뀌지 않는다. 바꾸려면 볼륨을 지우고 다시 띄우거나 DB 안에서 `ALTER USER`를 실행한다.
- 다른 프로젝트가 5432를 쓰고 있으면 `.env`의 `POSTGRES_PORT`와 `DATABASE_URL`의 포트를 함께 바꾼다.

## 미정 사항

- DB 종류와 ORM·마이그레이션 도구 (`app/db/`는 빈 골격)
- 엔드포인트 URL 설계 (기능 라우터의 prefix는 엔드포인트 설계 때 정한다)
- 인증 방식 (`app/api/deps.py`)
