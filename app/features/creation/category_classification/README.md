# CREATE-US-09 재분류 배치

- 기본 정보 직접 저장과 공통 저장(임시본 확정 포함)은 같은 `story_basic.apply_basic`을 거친다. 확정 세계관 또는 캐릭터 상세·구성이 달라지면 같은 트랜잭션에서 스토리를 분류 필요로 표시한다. 캐릭터 이름·정렬만 바뀌거나 기타 입력이 바뀌는 경우는 표시하지 않는다. 등록 스토리의 임시 편집본은 확정 전에는 읽지 않는다.
- `run_cycle`은 기존 유효 결과가 있고 분류 필요로 표시된 활성 등록 스토리를 주기당 한 번 수집한다. 실제 분류 시 최신 확정본의 세계관·캐릭터 상세만 전달한다. 성공한 최신 결과만 스토리 단위로 교체하고 표시를 해제한다.
- 시간 재분류의 일시 오류는 같은 주기에 바로 재시도한다. 확정 분류 입력의 변경 한 건당 재시작을 포함해 총 3회까지 시도하고, 영구 오류·잘못된 결과는 즉시 종료한다. 종료된 실패는 이후 주기에 다시 선택하지 않으며 기존 결과와 분류 필요 표시는 보존한다. 새로운 확정 분류 입력 변경이 있어야 다시 시도한다. 재분류 시도 기록은 운영용이며 탐색 노출 판정에 쓰지 않는다.
- 기본 `app.main:app`은 최초 작업을 등록 트랜잭션에 기록하고, 별도 실행기가 이를 처리한다. `create_app(category_classifier=...)`에 분류기가 주입된 경우에는 확정 저장 응답 뒤 즉시 처리를 시도할 수도 있다. 최초 분류와 재분류는 같은 입력 범위·결과 저장 규칙을 사용한다.
- 최초 등록 트랜잭션에 작업 기록을 남긴다. `recover_first_jobs`는 재시작 뒤 대기·중단 작업을 이어받는다. 시도는 공급자 호출 전 기록하고, 408·429·5xx 및 연결 오류는 1시간을 기다리지 않고 총 3회까지 실행한다. 잘못된 결과나 그 외 HTTP 오류는 재시도하지 않는다. 실패 후 분류 입력을 실제로 편집하면 해당 최초 작업이 다시 대기한다.

## 별도 실행기

기존 DB에는 `app/db/migrations/create_us09_first_classification_jobs.sql`과 `app/db/migrations/create_us09_reclassification_attempts.sql`을 순서대로 서버·워커 갱신 전에 적용한다.

적용 전제(카테고리 기준선, [Confluence 5210139](https://charchat.atlassian.net/wiki/spaces/CHARCHAT/pages/5210139) 참고):

- 새로 설치하는 환경은 `app/db/schema.sql`로 만든다. 위 마이그레이션은 필요 없다.
- 기존 환경에는 `stories.category_classification_needed boolean NOT NULL`, `categories`, `story_categories`가 이미 있어야 한다. 없으면 서버·워커를 띄우기 전에 승인된 카테고리 기준선을 먼저 적용한다. `create_us09_reclassification_attempts.sql`은 기준선이 없으면 아무것도 만들지 않고 예외로 중단한다.
- 예전에 쓰던 일회성 카테고리 스크립트를 원격 환경에서 실행하지 않는다.
- 같은 이름의 표가 이미 있으면 열 형태와 `PRIMARY KEY (story_id)`, `stories(id)` 참조, `attempt_token` 유일성, CHECK 식을 확인하고, 다르면 전체를 되돌린다. API와 같은 DB를 가리키는 `DATABASE_URL`, `CATEGORY_OPENROUTER_MODEL`(예: `openai/gpt-4.1-mini`), `CATEGORY_OPENROUTER_API_KEY`를 설정한 후 API와 별도의 프로세스로 실행한다.

```sh
python -m app.features.creation.category_classification.runtime
```

- DB 세션 잠금으로 실행기를 DB당 하나로 제한한다. 설정이나 작업 테이블이 없거나 중복 인스턴스면 오류로 종료한다. 리더 연결 손실은 작업·시도 경계에서 감지하며 이미 전송한 요청은 취소하지 않는다.
- 최초 작업은 시작 직후와 이후 15초 간격으로 확인한다. 재분류 주기는 프로세스 시작 시점부터 3,600초이며, 재시작 시 다시 센다. 이미 실행 중인 작업이 마감을 넘기면 다음 작업 전에 배치를 실행하고 놓친 회차를 몰아서 재실행하지 않는다.
- `SIGINT`·`SIGTERM` 후 새 작업이나 재시도를 시작하지 않는다. 실행 중인 시도는 결과를 정리한다. 남은 재시도가 있는 최초 분류는 다음 복구 때, 시간 재분류는 다음 배치 때 이어받는다.
- 배포 환경에서 이 명령을 별도 서비스로 실행해야 한다. 기존 등록 스토리 중 최초 결과와 작업 기록이 모두 없는 건 백필 없이는 복구 대상이 아니다.

## AC08 인계: 탐색 노출 기준

- 현재 서버 `dev`에는 홈·랭킹·추천 엔드포인트가 없고 앱 홈은 자리표시자다. 이 스토리는 그 노출면을 구현하지 않았으며, 아래는 해당 기능을 만들 때의 기준이다.
- 탐색 노출 여부는 유효 결과 연결만으로 판단한다: `EXISTS (SELECT 1 FROM story_categories sc WHERE sc.story_id = s.id)`(카테고리 필터가 있으면 `sc.category_code`까지 조건). `story_categories`는 `categories(code)` FK로 유효 코드만 담는다. `category_classification_needed` 표시나 `story_first_classification_jobs` 상태로 노출을 정하지 않는다.
- 이 조건은 각 기능이 정한 일반 필터(공개·미삭제 등)와 함께 걸고, 그 뒤에 순위 계산과 `LIMIT`을 적용한다. 순위·개수를 먼저 자른 뒤 거르지 않는다.
- 이 기준은 백필을 뜻하지 않는다. 작업 기록이 없는 기존 등록 스토리는 결과가 생기기 전까지 탐색에서 빠진다.
- 이 조건은 탐색 노출에만 쓴다. 직접 상세 진입·대화 시작·이어하기·메시지 전송은 분류 여부로 막지 않는다(AC06, `tests/features/conversation/resume/test_category_nonblocking.py`).
- QA 확인 표:

  | 상황 | 탐색 노출 | 직접 진입·대화 |
  | --- | --- | --- |
  | 미분류(작업 대기·실행 중·실패, 결과 연결 없음) | 제외 | 가능 |
  | 유효 결과 있음 | 포함(일반 필터 통과 시) | 가능 |
  | 기존 결과 있음 + 재분류 대기·실패 | 기존 결과로 계속 포함 | 가능 |
