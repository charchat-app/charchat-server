-- CHAT-US-13 (KAN-23) 대화 중 모델 변경. KAN-17(kan17_conversation_messaging.sql) 적용 뒤
-- 서버 배포 전에 기존 DB에 독립적으로 적용한다. 절차 및 롤백: docs/kan23-model-change-rollout.md
--
-- conversation_generations에 공급자가 알려 준 실제 모델 ID(reported_model, NULL 허용)만 더한다.
-- 기존 생성 행은 그대로 두고 reported_model은 NULL로 남는다(기본값 없음 → 표 다시 쓰기 없음).
-- 이미 같은 이름의 컬럼이 다른 정의로 있으면 예외로 전체를 되돌린다. 다시 실행해도 결과가 같다.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

DO $kan23$
BEGIN
    IF to_regclass('public.conversation_generations') IS NULL THEN
        RAISE EXCEPTION 'KAN-23: public.conversation_generations not found'
            USING HINT = 'Apply app/db/migrations/kan17_conversation_messaging.sql first.';
    END IF;
END;
$kan23$;

-- 첫 적용과 재실행 모두 같은 잠금 안에서 검사·변경하여 배포 작업이 경합하지 않게 한다.
LOCK TABLE public.conversation_generations IN ACCESS EXCLUSIVE MODE;

ALTER TABLE public.conversation_generations ADD COLUMN IF NOT EXISTS reported_model text;

DO $kan23$
DECLARE
    actual text;
BEGIN
    SELECT format('%s%s%s', format_type(a.atttypid, a.atttypmod),
                  CASE WHEN a.attnotnull THEN ' NOT NULL' END,
                  ' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid))
      INTO actual
      FROM pg_attribute a
      LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE a.attrelid = 'public.conversation_generations'::regclass
       AND a.attname = 'reported_model' AND NOT a.attisdropped;
    IF actual IS DISTINCT FROM 'text' THEN
        RAISE EXCEPTION 'KAN-23: unexpected public.conversation_generations.reported_model definition'
            USING DETAIL = format(E'expected: text (nullable, no default)\nactual: %s', actual);
    END IF;
END;
$kan23$;

COMMIT;
