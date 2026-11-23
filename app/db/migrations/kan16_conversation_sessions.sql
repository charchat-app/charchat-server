-- CHAT-US-01 (KAN-16). API 배포 전 기존 DB에 독립적으로 적용한다.
-- 절차 및 롤백: docs/kan16-conversation-sessions-rollout.md
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

-- 첫 적용과 재실행 모두 같은 잠금 안에서 검사·변경하여 배포 작업이 경합하지 않게 한다.
LOCK TABLE public.conversation_sessions IN ACCESS EXCLUSIVE MODE;

ALTER TABLE public.conversation_sessions
    ADD COLUMN IF NOT EXISTS intro_messages_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS initial_request_fingerprint bytea;

DO $kan16$
DECLARE
    sessions CONSTANT regclass := 'public.conversation_sessions'::regclass;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute a
        JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = sessions AND a.attname = 'intro_messages_snapshot'
          AND NOT a.attisdropped AND a.atttypid = 'jsonb'::regtype AND a.attnotnull
          AND a.attidentity = '' AND a.attgenerated = ''
          AND pg_get_expr(d.adbin, d.adrelid) = '''[]''::jsonb'
    ) THEN
        RAISE EXCEPTION 'KAN-16: unexpected intro_messages_snapshot column definition';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = sessions AND a.attname = 'initial_request_fingerprint'
          AND NOT a.attisdropped AND a.atttypid = 'bytea'::regtype AND NOT a.attnotnull
          AND a.attidentity = '' AND a.attgenerated = '' AND NOT a.atthasdef
    ) THEN
        RAISE EXCEPTION 'KAN-16: unexpected initial_request_fingerprint column definition';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = sessions
          AND conname = 'conversation_sessions_intro_messages_snapshot_check'
    ) THEN
        ALTER TABLE public.conversation_sessions
            ADD CONSTRAINT conversation_sessions_intro_messages_snapshot_check
                CHECK (jsonb_typeof(intro_messages_snapshot) = 'array');
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint c WHERE c.conrelid = sessions
          AND c.conname = 'conversation_sessions_intro_messages_snapshot_check'
          AND c.contype = 'c' AND c.convalidated AND NOT c.connoinherit
          AND pg_get_constraintdef(c.oid) = 'CHECK ((jsonb_typeof(intro_messages_snapshot) = ''array''::text))'
    ) THEN
        RAISE EXCEPTION 'KAN-16: unexpected snapshot check constraint definition';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = sessions
          AND conname = 'conversation_sessions_initial_request_key'
    ) THEN
        ALTER TABLE public.conversation_sessions
            ADD CONSTRAINT conversation_sessions_initial_request_key
                UNIQUE (owner_id, story_id, initial_request_fingerprint);
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_index i ON i.indexrelid = c.conindid AND i.indrelid = c.conrelid
        WHERE c.conrelid = sessions AND c.conname = 'conversation_sessions_initial_request_key'
          AND c.contype = 'u' AND c.convalidated AND NOT c.condeferrable
          AND i.indisunique AND i.indisvalid AND i.indisready AND i.indislive
          AND pg_get_constraintdef(c.oid) = 'UNIQUE (owner_id, story_id, initial_request_fingerprint)'
    ) THEN
        RAISE EXCEPTION 'KAN-16: unexpected initial request unique constraint definition';
    END IF;
END;
$kan16$;

COMMIT;
