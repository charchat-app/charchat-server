# CHAT-US-02 (KAN-17) 대화 메시지·응답 생성 DB 배포

## 적용 순서: DB 마이그레이션 → 서버 → 앱

KAN-17 서버는 세션 조회(`GET /conversation/sessions/{id}`)에서도 `conversation_sessions.model`을
읽는다. 컬럼이 없는 DB에 서버를 먼저 올리면 **기존 방 조회까지 실패**하므로 반드시 SQL을 먼저
적용한다. 앱은 서버 배포가 끝난 뒤 올린다.

1. 대상 DB, 백업/복구 수단, 배포 권한을 확인한다. 대상에는 KAN-16
   (`app/db/migrations/kan16_conversation_sessions.sql`)까지 적용된 스키마가 있어야 한다.
   `app/db/schema.sql`은 신규 로컬·테스트 DB 초기화용이며 기존 DB에 실행하지 않는다.
2. 적용 전 백업: `pg_dump -Fc --dbname="$DB_CONNINFO" > before-kan17.dump`
3. 저장소 루트에서 아래 파일을 별도 배포 단계로 실행한다. `DB_CONNINFO`는 운영자가 별도로
   설정한 접속 식별자(예: `service=<migration-service>`)다. 비밀값은 파일이나 명령에 적지 않는다.
   파일이 스스로 트랜잭션을 열고 닫으므로 외부 트랜잭션으로 감싸지 않는다.

   ```sh
   psql -X --dbname="$DB_CONNINFO" --set=ON_ERROR_STOP=1 \
     --file=app/db/migrations/kan17_conversation_messaging.sql
   ```

4. `COMMIT` 성공을 확인한 뒤 KAN-17 서버를 올린다. 확인:
   `SELECT to_regclass('conversation_messages'), to_regclass('conversation_generations');`
   두 값이 모두 나오고 `\d conversation_sessions`에 nullable `model text`가 있으면 된다.
5. 서버 배포 후 기존 방 조회(`GET /sessions/{id}`, `GET /sessions/{id}/turns`)와 새 메시지
   전송을 확인한 뒤 앱을 배포한다.

## 하는 일

- `conversation_sessions.model text` 추가(`ADD COLUMN IF NOT EXISTS`, NULL 허용·기본값 없음).
  기본값이 없는 nullable 컬럼이라 기존 행을 다시 쓰지 않는다. 기존 방은 `NULL`이다.
- `conversation_messages`, `conversation_generations` 생성(`CREATE TABLE IF NOT EXISTS`).
- `conversation_generations_one_active_idx`(세션당 pending·streaming 1개, 부분 유일 인덱스)와
  `conversation_generations_session_idx` 생성(`CREATE INDEX IF NOT EXISTS`). 새 빈 테이블에
  만드는 인덱스라 `CONCURRENTLY`가 필요 없다.
- 끝에서 컬럼·제약·인덱스 정의가 `schema.sql`과 같은지 검사한다. `IF NOT EXISTS`는 같은 이름의
  다른 정의를 그냥 건너뛰기 때문이다. 다르면 예외로 전체가 되돌아가며, 그때는 서버를 배포하지 않고
  원인을 확인한다.

`ADD COLUMN` 때문에 `conversation_sessions`에 `ACCESS EXCLUSIVE` 잠금을 잠깐 잡는다. 잠금 대기
3초, 문장 실행 60초로 제한한다. 실패하면 자동 재시도하거나 제한을 높이지 말고 원인을 확인한 뒤
파일을 처음부터 다시 실행한다. 이미 적용된 DB에 다시 실행해도 기존 데이터와 정의를 바꾸지 않는다.

## 기존 방과 모델 값

- 기존 방의 `model`은 `NULL`이다. 조회 API는 `NULL`을 그대로(`GET /sessions/{id}`) 또는 서버
  기본 모델로 표시(`GET /sessions/{id}/turns`)하고, **첫 응답 생성 요청 때**
  `CHAT_DEFAULT_MODEL` 값을 세션에 저장한다. 이후 그 방은 저장된 모델을 계속 쓴다.
- `CHAT_DEFAULT_MODEL` 기본값은 `fake-echo`(가짜 공급자용)다. 공용·운영 서버에서
  `CHAT_DEFAULT_MODEL`을 바꾸지 않고 가짜 공급자로 운영하면 그동안 첫 메시지를 보낸 방에는
  `fake-echo`가 저장되고, 나중에 기본 모델을 바꿔도 그 방들은 바뀌지 않는다. 실제 모델이 정해지면
  배포 전에 `CHAT_DEFAULT_MODEL`을 먼저 설정한다. 이미 `fake-echo`가 저장된 방을 바꿀지는
  모델 정책 결정 범위이며 이 배포에 포함하지 않는다.
- 과거 메시지 백필은 하지 않는다(대화 이력 테이블은 이 배포부터 쌓인다).

## 롤백: 서버 먼저, DB 데이터 보존

문제가 생기면 먼저 서버를 이전 버전(KAN-16)으로 되돌린다. 이전 서버는 새 컬럼·테이블을 쓰지
않으므로 그대로 두어도 된다. 새 테이블에는 이 배포 뒤 쌓인 대화 이력이 있으므로 제거는 기본 롤백에
포함하지 않는다. 데이터를 버리기로 결정한 경우에만, 서버를 되돌린 뒤 아래를 실행한다.

```sql
BEGIN;
DROP TABLE conversation_generations, conversation_messages;
ALTER TABLE conversation_sessions DROP COLUMN model;
COMMIT;
```
