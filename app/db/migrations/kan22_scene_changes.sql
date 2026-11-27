-- CHAT-US-08 (KAN-22) 장면 변경 기록 테이블. KAN-17(kan17_conversation_messaging.sql) 적용 뒤
-- 서버 배포 전에 기존 DB에 독립적으로 적용한다. 기존 표는 바꾸지 않고 새 표 하나만 더한다.
-- 다시 실행해도 결과가 같다. 같은 이름의 표가 다른 모양으로 있으면 예외로 전체를 되돌린다.
-- 되돌리기: 서버를 이전 버전으로 돌린 뒤 DROP TABLE public.conversation_scene_changes;
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

DO $kan22$
BEGIN
    IF to_regclass('public.conversation_generations') IS NULL
       OR to_regclass('public.conversation_sessions') IS NULL THEN
        RAISE EXCEPTION 'KAN-22: conversation tables not found'
            USING HINT = 'Apply app/db/migrations/kan17_conversation_messaging.sql first.';
    END IF;
END;
$kan22$;

CREATE TABLE IF NOT EXISTS public.conversation_scene_changes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES public.conversation_sessions(id) ON DELETE CASCADE,
    generation_id bigint NOT NULL REFERENCES public.conversation_generations(id),
    assistant_message_id bigint NOT NULL,
    evidence_message_id bigint NOT NULL,
    evidence_from text NOT NULL CHECK (evidence_from IN ('user', 'assistant')),
    evidence text NOT NULL DEFAULT '',
    before_state jsonb NOT NULL,
    after_state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DO $kan22$
DECLARE
    actual text;
    idx regclass;
    expected text := 'after_state jsonb NOT NULL,assistant_message_id bigint NOT NULL,'
        || 'before_state jsonb NOT NULL,created_at timestamp with time zone NOT NULL,'
        || 'evidence text NOT NULL,evidence_from text NOT NULL,'
        || 'evidence_message_id bigint NOT NULL,generation_id bigint NOT NULL,'
        || 'id bigint NOT NULL,session_id bigint NOT NULL';
BEGIN
    SELECT string_agg(
               format('%s %s%s', a.attname, format_type(a.atttypid, a.atttypmod),
                      CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END),
               ',' ORDER BY a.attname)
      INTO actual
      FROM pg_attribute a
     WHERE a.attrelid = 'public.conversation_scene_changes'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'KAN-22: conversation_scene_changes has unexpected columns: %', actual;
    END IF;
    SELECT string_agg(pg_get_constraintdef(c.oid), ' | ' ORDER BY pg_get_constraintdef(c.oid))
      INTO actual
      FROM pg_constraint c
     WHERE c.conrelid = 'public.conversation_scene_changes'::regclass
       AND c.contype <> 'n';  -- PostgreSQL 18의 NOT NULL 제약은 컬럼 검사에서 이미 본다
    IF actual IS DISTINCT FROM
        'CHECK ((evidence_from = ANY (ARRAY[''user''::text, ''assistant''::text])))'
        || ' | FOREIGN KEY (generation_id) REFERENCES conversation_generations(id)'
        || ' | FOREIGN KEY (session_id) REFERENCES conversation_sessions(id) ON DELETE CASCADE'
        || ' | PRIMARY KEY (id)' THEN
        RAISE EXCEPTION 'KAN-22: conversation_scene_changes has unexpected constraints: %', actual;
    END IF;
    idx := to_regclass('public.conversation_scene_changes_session_idx');
    IF idx IS NOT NULL AND pg_get_indexdef(idx) NOT LIKE
        'CREATE INDEX conversation_scene_changes_session_idx ON %conversation_scene_changes'
        || ' USING btree (session_id, id)' THEN
        RAISE EXCEPTION 'KAN-22: conversation_scene_changes_session_idx has an unexpected definition';
    END IF;
END;
$kan22$;

CREATE INDEX IF NOT EXISTS conversation_scene_changes_session_idx
    ON public.conversation_scene_changes (session_id, id);

COMMIT;
