# CHAT-US-13 (KAN-23) 대화 중 모델 변경 DB 배포

KAN-23은 `conversation_generations`에 공급자가 알려 준 실제 모델 ID 컬럼 하나만 더한다.
요청 모델은 기존 `conversation_generations.model_id`(생성 행을 만들 때의 세션 선택 모델)가 기록한다.
모델 변경 이력 표는 만들지 않는다.

| 객체 | 내용 |
| --- | --- |
| `conversation_generations.reported_model text NULL` | 공급자 응답의 실제 모델 ID(예: OpenAI SSE 조각의 `model`). 알려 주지 않았거나 첫 조각 전에 실패하면 NULL |

## 적용 순서: DB 마이그레이션 → 서버 → 앱

KAN-23 서버는 생성 조회·기록에서 `reported_model`을 읽고 쓴다. SQL을 먼저 적용하지 않고 서버를
올리면 메시지 전송·생성 조회가 실패한다. 앱은 서버 배포가 끝난 뒤 올린다.

1. 대상 DB, 백업/복구 수단, 배포 권한을 확인한다. 대상에는 KAN-17
   (`app/db/migrations/kan17_conversation_messaging.sql`)까지 적용돼 있어야 한다.
   **공용 DB 적용은 승인 뒤에만 한다.**
2. 적용 전 백업: `pg_dump -Fc --dbname="$DB_CONNINFO" > before-kan23.dump`
3. 저장소 루트에서 실행한다. 파일이 스스로 트랜잭션을 열고 닫으므로 외부 트랜잭션으로 감싸지 않는다.

   ```sh
   psql -X --dbname="$DB_CONNINFO" --set=ON_ERROR_STOP=1 \
     --file=app/db/migrations/kan23_model_change.sql
   ```

4. 확인: `SELECT reported_model FROM conversation_generations LIMIT 1;` 이 오류 없이 실행된다.

## 파일이 하는 일

- `conversation_generations`가 없으면(KAN-17 미적용) 예외로 중단한다.
- `conversation_generations`를 ACCESS EXCLUSIVE로 잠그고 `ADD COLUMN IF NOT EXISTS reported_model text`.
  기본값이 없는 NULL 허용 컬럼이라 표를 다시 쓰지 않는다. 기존 행은 NULL로 남는다.
- 같은 이름의 컬럼이 다른 정의(타입·NOT NULL·기본값)로 있으면 예외로 전체를 되돌린다.
- 다시 실행해도 결과가 같다. `lock_timeout 3s`, `statement_timeout 60s`.

## 주의

- KAN-23 적용 뒤에 `kan17_conversation_messaging.sql`을 다시 실행하면 KAN-17 파일의
  `conversation_generations` 정의 검사가 새 컬럼 때문에 `KAN-17: unexpected ... definition`으로
  중단되고 전체가 되돌아간다(데이터 변경 없음). KAN-17 재실행이 필요하면 KAN-23 적용 전에 한다.

## 롤백

순서: **앱 → 서버 → DB.** 앱과 서버를 KAN-23 이전 버전으로 되돌린 뒤, 필요하면 컬럼을 지운다
(기록된 실제 모델 ID는 사라진다). 컬럼 삭제도 표 잠금(ACCESS EXCLUSIVE)이 필요하므로 짧은 잠금 제한을 둔다.

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
ALTER TABLE conversation_generations DROP COLUMN IF EXISTS reported_model;
COMMIT;
```

적용·롤백 중 `lock_timeout`으로 실패하면 트랜잭션 전체가 되돌아가 아무것도 바뀌지 않는다.
사용이 적은 시간에 그대로 다시 실행하면 된다.

KAN-17 파일은 처음 한 번 사람이 직접 실행하는 파일이다. 배포 때마다 모든 마이그레이션을 다시
실행하는 절차에 넣지 않는다(위 「주의」 참고).
