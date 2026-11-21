# CHAT-US-01 (KAN-16) 대화 세션 DB 배포

## 적용 순서: DB → API

1. 대상 DB, 백업/복구 수단, 배포 권한을 확인한다. 대상에는 KAN-16 이전의
   `public.conversation_sessions` 스키마가 있어야 한다. 운영과 같은 버전과 유사한
   세션 수의 복제본에서 적용 시간과 잠금 영향을 먼저 확인한다. `app/db/schema.sql`은
   신규 로컬·테스트 DB 초기화용이며 기존 DB에 실행하지 않는다.
2. 저장소 루트에서 아래 파일을 별도 배포 단계로 실행한다. `DB_CONNINFO`는 운영자가
   별도로 설정한 접속 식별자(예: `service=<migration-service>`)다. 비밀값은 파일이나
   명령에 적지 않는다. 외부 트랜잭션으로 감싸지 않는다.

   ```sh
   psql -X --dbname="$DB_CONNINFO" --set=ON_ERROR_STOP=1 \
     --file=app/db/migrations/kan16_conversation_sessions.sql
   ```

3. `COMMIT` 성공을 확인한 뒤 KAN-16 API를 올린다. `\d public.conversation_sessions`로
   `intro_messages_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb`와 배열 체크,
   nullable `initial_request_fingerprint bytea`(기본값 없음), 그리고
   `conversation_sessions_initial_request_key`의 `(owner_id, story_id, initial_request_fingerprint)`
   고유 제약을 확인한다. 배포 후 접근 권한이 있는 기존 방의 조회와 새 방 시작/동일
   요청 재시도를 확인한다.

SQL은 한 트랜잭션으로 실행된다. 배열 제약 검증과 고유 인덱스 생성까지 테이블의
읽기·쓰기를 막는 `ACCESS EXCLUSIVE` 잠금을 사용하므로 트래픽이 적은 시간에 적용한다.
잠금 대기는 3초, 문장 실행은 60초로 제한한다. 잠금/실행 시간 초과, 제약 위반,
동일 이름의 다른 정의 등으로 실패하면 API를 배포하지 않는다. 원인을 확인하고 해소한 뒤
파일을 처음부터 다시 실행한다. 대상 규모상 잠금 허용 시간을 넘기면 자동 재시도하거나
제한을 높이지 말고 별도 배포 계획을 검토한다. 이미 동일한 정의가 적용됐다면 재실행해도
기존 데이터와 제약을 바꾸지 않는다.

## 기존 방과 스냅샷

- 저장된 스냅샷이 없는 기존 방은 `intro_messages_snapshot = []`, 요청 지문은 `NULL`이다.
  방 조회는 기존 동작대로 `messages: []`를 반환한다. 원본 인트로가 남아 있어도 편집될 수
  있고, 삭제되면 연결 자체가 없어지므로 현재 텍스트를 과거 대화 이력으로 복원하지 않는다.
  지문이 없는 방은 새 요청을 재시도할 때 일치 대상으로 삼지 않는다.
- 이미 캡처된 스냅샷과 지문은 덮어쓰지 않는다. 새 방은 생성 시점의 표시 텍스트를
  저장하므로 이후 원본을 편집 또는 삭제해도 스냅샷은 유지된다.
- **제품 백필 정책은 변경하지 않았다. 과거 메시지 복원·백필은 승인되지 않았으며 이 배포에 포함하지 않는다.**

## 롤백: API 먼저, DB 데이터 보존

문제가 생기면 먼저 API를 이전 버전으로 되돌리고 KAN-16 API 인스턴스를 모두 내린다.
추가 컬럼과 제약은 기본적으로 남겨 둔다. 이전 쓰기는 새 필드를 지정하지 않아도 되고,
새로 기록된 스냅샷과 요청 지문은 재배포 때 그대로 사용할 수 있다. 컬럼 제거는 캡처된
데이터를 잃는 별도 작업이므로 기본 롤백에 포함하지 않는다.
