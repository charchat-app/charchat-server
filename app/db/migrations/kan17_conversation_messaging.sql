-- CHAT-US-02 (KAN-17). API 배포 전 기존 DB에 독립적으로 적용한다(KAN-16 SQL 적용 후).
-- 절차 및 롤백: docs/kan17-conversation-messaging-rollout.md
-- 기존 행은 바꾸지 않는다. 여러 번 실행해도 같은 결과다(IF NOT EXISTS + 정의 검사).
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

-- 첫 적용과 재실행 모두 같은 잠금 안에서 검사·변경하여 배포 작업이 경합하지 않게 한다.
LOCK TABLE public.conversation_sessions IN ACCESS EXCLUSIVE MODE;

-- 세션에 적용한 모델. 기존 방은 NULL이며 첫 응답 생성 요청 때 CHAT_DEFAULT_MODEL로 채운다.
ALTER TABLE public.conversation_sessions ADD COLUMN IF NOT EXISTS model text;

CREATE TABLE IF NOT EXISTS public.conversation_messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES public.conversation_sessions(id) ON DELETE CASCADE,
    seq integer NOT NULL CHECK (seq > 0),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    parts jsonb NOT NULL CHECK (jsonb_typeof(parts) = 'array'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT conversation_messages_session_seq_unique UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS public.conversation_generations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES public.conversation_sessions(id) ON DELETE CASCADE,
    owner_id bigint NOT NULL,
    request_id text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'streaming', 'completed', 'failed', 'failed_partial', 'save_failed'
    )),
    user_message_id bigint NOT NULL
        REFERENCES public.conversation_messages(id) ON DELETE CASCADE,
    assistant_message_id bigint REFERENCES public.conversation_messages(id) ON DELETE SET NULL,
    text text NOT NULL DEFAULT '',
    model text,
    error_code text,
    billable boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT conversation_generations_request_unique UNIQUE (owner_id, session_id, request_id)
);

-- 세션마다 진행 중(pending·streaming) 생성은 하나만 허용한다.
CREATE UNIQUE INDEX IF NOT EXISTS conversation_generations_one_active_idx
    ON public.conversation_generations(session_id) WHERE status IN ('pending', 'streaming');
CREATE INDEX IF NOT EXISTS conversation_generations_session_idx
    ON public.conversation_generations(session_id, id);

-- IF NOT EXISTS는 같은 이름의 다른 정의를 건너뛰므로, 적용 결과가 기대 정의와 같은지 확인한다.
DO $kan17$
DECLARE
    sessions CONSTANT regclass := 'public.conversation_sessions'::regclass;
    messages CONSTANT regclass := 'public.conversation_messages'::regclass;
    generations CONSTANT regclass := 'public.conversation_generations'::regclass;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = sessions AND a.attname = 'model' AND NOT a.attisdropped
          AND a.atttypid = 'text'::regtype AND NOT a.attnotnull AND NOT a.atthasdef
          AND a.attidentity = '' AND a.attgenerated = ''
    ) THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_sessions.model column definition';
    END IF;

    IF (
        SELECT string_agg(
            a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
                || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END
                || CASE WHEN a.attidentity <> '' THEN ' identity' ELSE '' END
                || coalesce(' default ' || pg_get_expr(d.adbin, d.adrelid), ''),
            ', ' ORDER BY a.attnum)
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = messages AND a.attnum > 0 AND NOT a.attisdropped
    ) IS DISTINCT FROM
        'id bigint not null identity, session_id bigint not null, seq integer not null, '
        'role text not null, parts jsonb not null, '
        'created_at timestamp with time zone not null default clock_timestamp()'
    THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_messages columns';
    END IF;

    IF (
        SELECT string_agg(
            a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
                || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END
                || CASE WHEN a.attidentity <> '' THEN ' identity' ELSE '' END
                || coalesce(' default ' || pg_get_expr(d.adbin, d.adrelid), ''),
            ', ' ORDER BY a.attnum)
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = generations AND a.attnum > 0 AND NOT a.attisdropped
    ) IS DISTINCT FROM
        'id bigint not null identity, session_id bigint not null, owner_id bigint not null, '
        'request_id text not null, status text not null default ''pending''::text, '
        'user_message_id bigint not null, assistant_message_id bigint, '
        'text text not null default ''''::text, model text, error_code text, '
        'billable boolean not null default false, '
        'created_at timestamp with time zone not null default clock_timestamp(), '
        'updated_at timestamp with time zone not null default clock_timestamp()'
    THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_generations columns';
    END IF;

    IF (
        SELECT string_agg(c.conname || ' ' || pg_get_constraintdef(c.oid), '; ' ORDER BY c.conname)
        FROM pg_constraint c WHERE c.conrelid = messages AND c.convalidated
    ) IS DISTINCT FROM
        'conversation_messages_parts_check CHECK ((jsonb_typeof(parts) = ''array''::text)); '
        'conversation_messages_pkey PRIMARY KEY (id); '
        'conversation_messages_role_check '
        'CHECK ((role = ANY (ARRAY[''user''::text, ''assistant''::text]))); '
        'conversation_messages_seq_check CHECK ((seq > 0)); '
        'conversation_messages_session_id_fkey FOREIGN KEY (session_id) '
        'REFERENCES conversation_sessions(id) ON DELETE CASCADE; '
        'conversation_messages_session_seq_unique UNIQUE (session_id, seq)'
    THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_messages constraints';
    END IF;

    IF (
        SELECT string_agg(c.conname || ' ' || pg_get_constraintdef(c.oid), '; ' ORDER BY c.conname)
        FROM pg_constraint c WHERE c.conrelid = generations AND c.convalidated
    ) IS DISTINCT FROM
        'conversation_generations_assistant_message_id_fkey FOREIGN KEY (assistant_message_id) '
        'REFERENCES conversation_messages(id) ON DELETE SET NULL; '
        'conversation_generations_pkey PRIMARY KEY (id); '
        'conversation_generations_request_unique UNIQUE (owner_id, session_id, request_id); '
        'conversation_generations_session_id_fkey FOREIGN KEY (session_id) '
        'REFERENCES conversation_sessions(id) ON DELETE CASCADE; '
        'conversation_generations_status_check CHECK ((status = ANY (ARRAY[''pending''::text, '
        '''streaming''::text, ''completed''::text, ''failed''::text, ''failed_partial''::text, '
        '''save_failed''::text]))); '
        'conversation_generations_user_message_id_fkey FOREIGN KEY (user_message_id) '
        'REFERENCES conversation_messages(id) ON DELETE CASCADE'
    THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_generations constraints';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        WHERE i.indexrelid = 'public.conversation_generations_one_active_idx'::regclass
          AND i.indrelid = generations AND i.indisvalid AND i.indisready AND i.indislive
          AND pg_get_indexdef(i.indexrelid) = 'CREATE UNIQUE INDEX '
              'conversation_generations_one_active_idx ON public.conversation_generations '
              'USING btree (session_id) WHERE (status = ANY '
              '(ARRAY[''pending''::text, ''streaming''::text]))'
    ) THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_generations_one_active_idx definition';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        WHERE i.indexrelid = 'public.conversation_generations_session_idx'::regclass
          AND i.indrelid = generations AND i.indisvalid AND i.indisready AND i.indislive
          AND pg_get_indexdef(i.indexrelid) = 'CREATE INDEX conversation_generations_session_idx '
              'ON public.conversation_generations USING btree (session_id, id)'
    ) THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_generations_session_idx definition';
    END IF;
END;
$kan17$;

COMMIT;
