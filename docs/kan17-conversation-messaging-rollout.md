# CHAT-US-02 (KAN-17) 대화 메시지·응답 생성 DB 배포

KAN-17은 팀이 승인한 대화 DB 구조(2026-11-20 공용 DB 적용, Confluence 3047447
「대화 1 · 모델·메시지 DB 마이그레이션 범위 및 적용 기록」)를 그대로 쓰고, 팀 표에 없는 생성 작업
정보만 추가 표 `conversation_generations` 하나에 둔다. 팀 표에는 컬럼을 더하지 않는다.

| 객체 | 출처 | 내용 |
| --- | --- | --- |
| `models` | 팀 DDL | 모델 목록(`name`, `provider`, `provider_model_id`) |
| `conversation_sessions.selected_model_id` + `conversation_sessions_selected_model_fk` | 팀 DDL | 세션에 고정한 모델(NOT NULL) |
| `conversation_messages` | 팀 DDL | 메시지(세션별 `position`, `role`, `status`) |
| `conversation_message_parts` | 팀 DDL | 메시지의 발화자별 부분 |
| `models` 기준 행 `루나`·`테라` | KAN-17 | `('루나','openai','gpt-6-luna')`, `('테라','openai','gpt-6.1-terra')` |
| `conversation_generations` (+ 인덱스 2개) | KAN-17 추가 | request_id 중복 방지, 메시지 연결, 상태·오류·스트리밍 원문·청구 표시 |

## 적용 순서: DB 마이그레이션 → 서버 → 앱

KAN-17 서버는 세션 시작·조회에서 `models`와 `selected_model_id`를, 메시지 API에서 새 표를 읽는다.
SQL을 먼저 적용하지 않고 서버를 올리면 **세션 시작과 기존 방 조회까지 실패**한다. 앱은 서버 배포가
끝난 뒤 올린다(앱 API 계약은 바뀌지 않았다).

1. 대상 DB, 백업/복구 수단, 배포 권한을 확인한다. 대상에는 KAN-16
   (`app/db/migrations/kan16_conversation_sessions.sql`)까지 적용돼 있어야 한다.
   `app/db/schema.sql`은 신규 로컬·테스트 DB 초기화용이며 기존 DB에 실행하지 않는다.
2. **공용 원격 DB에는 팀 DDL(2026-11-20)이 이미 적용돼 있다. 팀 DDL을 다시 실행하지 않는다**
   (`CREATE TABLE`·`ADD COLUMN`이 실패하거나, 다른 경로로 바뀐 정의를 덮을 수 있다).
   아래 마이그레이션 파일만 실행한다. 이 파일은 팀 표가 있으면 정의만 검사하고 건드리지 않는다.
3. 적용 전 백업: `pg_dump -Fc --dbname="$DB_CONNINFO" > before-kan17.dump`
4. 저장소 루트에서 아래 파일을 별도 배포 단계로 실행한다. `DB_CONNINFO`는 운영자가 별도로
   설정한 접속 식별자(예: `service=<migration-service>`)다. 비밀값은 파일이나 명령에 적지 않는다.
   파일이 스스로 트랜잭션을 열고 닫으므로 외부 트랜잭션으로 감싸지 않는다.

   ```sh
   psql -X --dbname="$DB_CONNINFO" --set=ON_ERROR_STOP=1 \
     --file=app/db/migrations/kan17_conversation_messaging.sql
   ```

5. `COMMIT` 성공을 확인한 뒤 KAN-17 서버를 올린다. 확인:

   ```sql
   SELECT name, provider, provider_model_id FROM models ORDER BY id;   -- 루나, 테라
   SELECT to_regclass('conversation_generations');                      -- not null
   ```

6. 서버 환경변수: `CHAT_DEFAULT_MODEL_NAME`(기본 `루나`)이 `models.name`과 정확히 한 행으로
   맞아야 세션을 시작할 수 있다(없거나 여러 행이면 `503 CONVERSATION_MODEL_NOT_CONFIGURED`).
   실제 호출은 `CHAT_LLM_PROVIDER=openai_compatible`, `CHAT_LLM_API_KEY`(비밀 저장소),
   `CHAT_LLM_BASE_URL`(기본 `https://api.openai.com/v1`). `fake`는 네트워크 없이 가짜 응답을 보낸다.
   OpenRouter로 호출하려면 `CHAT_LLM_BASE_URL=https://openrouter.ai/api/v1`,
   `CHAT_LLM_MODEL_PREFIX=openai/`로 둔다. DB의 `models.provider_model_id`는 바꾸지 않는다.
7. 서버 배포 후 기존 방 조회(`GET /sessions/{id}`, `GET /sessions/{id}/turns`), 새 세션 시작,
   메시지 전송을 확인한 뒤 앱을 배포한다.

## 파일이 하는 일

같은 잠금(`conversation_sessions` ACCESS EXCLUSIVE, `models` SHARE ROW EXCLUSIVE) 안에서 순서대로:

1. 이전 KAN-17 초안(`conversation_sessions.model` 컬럼)이 있으면 중단한다. 초안을 적용했던 로컬
   DB는 옮기지 않고 `schema.sql`로 다시 만든다.
2. `models`가 없으면 팀 정의 그대로 만들고, 정의를 검사한다. 기준 행은 **name이 없을 때만** 넣는다
   (이미 같은 이름이 있으면 그 행을 유지). `루나`가 정확히 한 행인지 확인한다.
3. `selected_model_id`가 없으면(팀 DDL 미적용 DB) nullable로 추가 → 기존 방을 모두 `루나`로 채움 →
   `SET NOT NULL` → `conversation_sessions_selected_model_fk` 추가. 이미 있으면 정의만 검사한다.
4. `conversation_messages` → `conversation_message_parts` → `conversation_generations`를
   `CREATE TABLE IF NOT EXISTS`로 만들고 각각 정의를 검사한다. 생성 표의 인덱스 두 개는 새 빈 표에
   만드는 것이라 `CONCURRENTLY`가 필요 없다.

정의 검사는 컬럼(타입·NOT NULL·identity·기본값), NOT NULL 외 제약 이름·정의, 제약 외 인덱스를
`schema.sql`로 만든 DB와 문자열로 비교한다. `IF NOT EXISTS`가 같은 이름의 다른 정의를 그냥 건너뛰기
때문이다. 다르면 `KAN-17: unexpected ... definition` 예외(DETAIL에 기대값/실제값)로 전체가
되돌아간다. 그때는 서버를 배포하지 않고 원인을 확인한다.

잠금 대기 3초, 문장 실행 60초로 제한한다. 실패하면 자동 재시도하거나 제한을 높이지 말고 원인을
확인한 뒤 파일을 처음부터 다시 실행한다. 이미 적용된 DB에 다시 실행해도 데이터와 정의를 바꾸지 않는다.

## 상태 대응

- 이용자 메시지: `role='user'`, `status='completed'`. 부분은 이용자가 고른 발화자 그대로
  (`user`=적용 프로필, `character`=`session_character_id`, `narrator`).
- AI 메시지: 생성 요청 때 `pending`으로 만들고 생성 상태를 따라간다.

  | conversation_generations.status | conversation_messages.status |
  | --- | --- |
  | pending / streaming / completed | 같은 값 |
  | failed (표시 내용 없이 실패) | failed |
  | failed_partial (일부 표시 후 중단, `STREAM_INTERRUPTED` 등) | interrupted |
  | save_failed (전체 생성 후 저장 실패) | failed |

  완료 때 파싱한 발화자별 부분을 `conversation_message_parts`에 저장한다(캐릭터는 세션 캐릭터
  이름 `name_snapshot`, 원본 최신 이름도 인정 → `session_character_id`).
- `position`은 세션마다 0부터 1씩 늘어난다(세션 행 잠금 안에서 `max+1`). 인트로는 메시지로 복사하지
  않는다. 재시도는 이용자 메시지를 다시 쓰고 AI 메시지만 새 순서로 만든다.
- 이력(`/turns`)과 모델 요청 이력은 `status='completed'` 메시지만 쓴다.

## 기존 방과 모델

- 팀 DDL이 없던 DB의 기존 방은 모두 `루나`로 채운다. 공용 DB는 팀 DDL 적용 당시 세션이 0건이었고,
  `models`가 비어 있는 동안에는 FK 때문에 세션이 생길 수 없다.
- 새 세션은 시작 때 `CHAT_DEFAULT_MODEL_NAME` 모델로 고정되고 이후 바뀌지 않는다. 모델 변경 기능은
  범위 밖이다.
- 과거 메시지 백필은 없다(이 배포부터 쌓인다).

## 롤백: 서버 먼저, DB 데이터 보존

문제가 생기면 먼저 서버를 이전 버전으로 되돌린다. 공용 DB의 팀 객체(`models`, `selected_model_id`,
`conversation_messages`, `conversation_message_parts`)는 팀 소유이며 **롤백 대상이 아니다.**
KAN-17이 추가한 것만 데이터를 버리기로 결정한 경우에 제거한다.

```sql
BEGIN;
DROP TABLE conversation_generations;
-- 기준 행 삭제는 이를 참조하는 세션·생성이 없을 때만 가능하다. 필요할 때만 따로 결정한다.
COMMIT;
```

KAN-16 이전 상태의 DB(팀 DDL 미적용)에서 팀 객체까지 되돌려야 한다면 팀과 먼저 합의한다.
