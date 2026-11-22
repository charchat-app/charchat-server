-- CHAT-US-02 (KAN-17). 서버 배포 전 기존 DB에 독립적으로 적용한다.
-- 절차 및 롤백: docs/kan17-conversation-messaging-rollout.md
--
-- 두 경우 모두 같은 결과(app/db/schema.sql과 같은 정의)가 되도록 한다.
--   (가) 팀 승인 DDL(2026-11-20)이 이미 적용된 공용 DB: 팀 표는 정의만 검사하고 바꾸지 않는다.
--        모델 기준 행이 없으면 넣고 conversation_generations만 새로 만든다.
--   (나) 팀 DDL이 없는 KAN-16 DB: 팀 표를 같은 이름·정의로 만들고, 기존 방의
--        selected_model_id는 기본 모델 '루나'로 채운 뒤 NOT NULL·FK를 건다.
-- 이미 있는 객체가 기대 정의와 다르면 예외로 전체를 되돌린다. 다시 실행해도 결과가 같다.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

-- 첫 적용과 재실행 모두 같은 잠금 안에서 검사·변경하여 배포 작업이 경합하지 않게 한다.
LOCK TABLE public.conversation_sessions IN ACCESS EXCLUSIVE MODE;

-- 표 정의 요약(종류, 컬럼, NOT NULL 외 제약, 제약 외 인덱스). 기대값과 문자열로 비교한다.
CREATE OR REPLACE FUNCTION pg_temp.kan17_signature(rel regclass) RETURNS text
LANGUAGE sql STABLE AS $sig$
    SELECT concat_ws(E'\n',
        (SELECT format('rel %s %s', c.relkind, c.relpersistence) FROM pg_class c WHERE c.oid = rel),
        (SELECT string_agg(format('col %s %s%s%s%s', a.attname,
                    format_type(a.atttypid, a.atttypmod),
                    CASE WHEN a.attnotnull THEN ' NOT NULL' END,
                    CASE a.attidentity WHEN 'a' THEN ' IDENTITY ALWAYS'
                                       WHEN 'd' THEN ' IDENTITY BY DEFAULT' END,
                    ' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid)), E'\n' ORDER BY a.attnum)
           FROM pg_attribute a
           LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
           WHERE a.attrelid = rel AND a.attnum > 0 AND NOT a.attisdropped),
        (SELECT string_agg(format('con %s %s', c.conname, pg_get_constraintdef(c.oid)), E'\n'
                    ORDER BY c.conname)
           FROM pg_constraint c WHERE c.conrelid = rel AND c.contype <> 'n'),
        (SELECT string_agg(format('idx %s', pg_get_indexdef(i.indexrelid)), E'\n'
                    ORDER BY pg_get_indexdef(i.indexrelid))
           FROM pg_index i
           WHERE i.indrelid = rel AND NOT EXISTS (
               SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid AND c.conrelid = rel)));
$sig$;

CREATE OR REPLACE FUNCTION pg_temp.kan17_expect(rel text, expected text) RETURNS void
LANGUAGE plpgsql AS $expect$
DECLARE
    actual text := pg_temp.kan17_signature(rel::regclass);
BEGIN
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'KAN-17: unexpected % definition', rel
            USING DETAIL = format(E'expected:\n%s\nactual:\n%s', expected, actual);
    END IF;
END;
$expect$;

-- 이전 KAN-17 초안(conversation_sessions.model, jsonb parts 메시지 표)을 적용한 로컬 DB는
-- 팀 구조로 옮기지 않는다. 그런 DB는 schema.sql로 다시 만든다.
DO $kan17$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'public.conversation_sessions'::regclass AND attname = 'model'
          AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION 'KAN-17: conversation_sessions.model found (old KAN-17 draft schema)'
            USING HINT = 'Rebuild this local DB from app/db/schema.sql; do not migrate it.';
    END IF;
END;
$kan17$;

-- 1. 모델 목록(팀 DDL)과 기준 행. 기준 행은 name으로 멱등 처리한다.
CREATE TABLE IF NOT EXISTS public.models (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    provider text NOT NULL,
    provider_model_id text NOT NULL
);
LOCK TABLE public.models IN SHARE ROW EXCLUSIVE MODE;
SELECT pg_temp.kan17_expect('public.models', $exp$rel r p
col id bigint NOT NULL IDENTITY ALWAYS
col name text NOT NULL
col provider text NOT NULL
col provider_model_id text NOT NULL
con models_pkey PRIMARY KEY (id)$exp$);

INSERT INTO public.models (name, provider, provider_model_id)
SELECT v.name, v.provider, v.provider_model_id
FROM (VALUES
    (1, '루나', 'openai', 'gpt-6-luna'),
    (2, '테라', 'openai', 'gpt-6.1-terra')
) AS v(ord, name, provider, provider_model_id)
WHERE NOT EXISTS (SELECT 1 FROM public.models m WHERE m.name = v.name)
ORDER BY v.ord;

DO $kan17$
BEGIN
    IF (SELECT count(*) FROM public.models WHERE name = '루나') <> 1 THEN
        RAISE EXCEPTION 'KAN-17: expected exactly one models row named 루나';
    END IF;
END;
$kan17$;

-- 2. 세션의 선택 모델(팀 DDL). 컬럼이 없으면 기존 방을 기본 모델 '루나'로 채운 뒤 NOT NULL로 바꾼다.
DO $kan17$
DECLARE
    sessions CONSTANT regclass := 'public.conversation_sessions'::regclass;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = sessions AND attname = 'selected_model_id' AND NOT attisdropped
    ) THEN
        ALTER TABLE public.conversation_sessions ADD COLUMN selected_model_id bigint;
        UPDATE public.conversation_sessions
            SET selected_model_id = (SELECT id FROM public.models WHERE name = '루나');
        ALTER TABLE public.conversation_sessions ALTER COLUMN selected_model_id SET NOT NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = sessions AND conname = 'conversation_sessions_selected_model_fk'
    ) THEN
        ALTER TABLE public.conversation_sessions
            ADD CONSTRAINT conversation_sessions_selected_model_fk
                FOREIGN KEY (selected_model_id) REFERENCES public.models(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = sessions AND a.attname = 'selected_model_id' AND NOT a.attisdropped
          AND a.atttypid = 'bigint'::regtype AND a.attnotnull AND NOT a.atthasdef
          AND a.attidentity = '' AND a.attgenerated = ''
    ) THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_sessions.selected_model_id definition';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = sessions AND c.conname = 'conversation_sessions_selected_model_fk'
          AND c.contype = 'f' AND c.convalidated
          AND pg_get_constraintdef(c.oid)
              = 'FOREIGN KEY (selected_model_id) REFERENCES models(id)'
    ) THEN
        RAISE EXCEPTION 'KAN-17: unexpected conversation_sessions_selected_model_fk definition';
    END IF;
END;
$kan17$;

-- 3. 메시지와 메시지 부분(팀 DDL). 부분 표가 메시지 표를 참조하므로 먼저 메시지 표를 검사한다.
CREATE TABLE IF NOT EXISTS public.conversation_messages (
    session_id bigint NOT NULL REFERENCES public.conversation_sessions(id),
    id bigint GENERATED ALWAYS AS IDENTITY,
    position bigint NOT NULL CHECK (position >= 0),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    status text NOT NULL CHECK (status IN ('pending', 'streaming', 'completed', 'failed', 'interrupted')),
    PRIMARY KEY (session_id, id),
    CONSTRAINT conversation_messages_position_unique UNIQUE (session_id, position)
);
SELECT pg_temp.kan17_expect('public.conversation_messages', $exp$rel r p
col session_id bigint NOT NULL
col id bigint NOT NULL IDENTITY ALWAYS
col position bigint NOT NULL
col role text NOT NULL
col status text NOT NULL
con conversation_messages_pkey PRIMARY KEY (session_id, id)
con conversation_messages_position_check CHECK (("position" >= 0))
con conversation_messages_position_unique UNIQUE (session_id, "position")
con conversation_messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text])))
con conversation_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES conversation_sessions(id)
con conversation_messages_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'streaming'::text, 'completed'::text, 'failed'::text, 'interrupted'::text])))$exp$);

CREATE TABLE IF NOT EXISTS public.conversation_message_parts (
    session_id bigint NOT NULL,
    message_id bigint NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    speaker_kind text NOT NULL CHECK (speaker_kind IN ('user', 'character', 'narrator')),
    session_character_id bigint,
    body text NOT NULL,
    PRIMARY KEY (session_id, message_id, position),
    CONSTRAINT conversation_message_parts_message_fk
        FOREIGN KEY (session_id, message_id)
        REFERENCES public.conversation_messages(session_id, id),
    CONSTRAINT conversation_message_parts_character_fk
        FOREIGN KEY (session_id, session_character_id)
        REFERENCES public.conversation_character_states(session_id, id),
    CONSTRAINT conversation_message_parts_speaker_check CHECK (
        (speaker_kind = 'character' AND session_character_id IS NOT NULL)
        OR (speaker_kind IN ('user', 'narrator') AND session_character_id IS NULL)
    )
);
SELECT pg_temp.kan17_expect('public.conversation_message_parts', $exp$rel r p
col session_id bigint NOT NULL
col message_id bigint NOT NULL
col position integer NOT NULL
col speaker_kind text NOT NULL
col session_character_id bigint
col body text NOT NULL
con conversation_message_parts_character_fk FOREIGN KEY (session_id, session_character_id) REFERENCES conversation_character_states(session_id, id)
con conversation_message_parts_message_fk FOREIGN KEY (session_id, message_id) REFERENCES conversation_messages(session_id, id)
con conversation_message_parts_pkey PRIMARY KEY (session_id, message_id, "position")
con conversation_message_parts_position_check CHECK (("position" >= 0))
con conversation_message_parts_speaker_check CHECK ((((speaker_kind = 'character'::text) AND (session_character_id IS NOT NULL)) OR ((speaker_kind = ANY (ARRAY['user'::text, 'narrator'::text])) AND (session_character_id IS NULL))))
con conversation_message_parts_speaker_kind_check CHECK ((speaker_kind = ANY (ARRAY['user'::text, 'character'::text, 'narrator'::text])))$exp$);

-- 4. 생성 작업(KAN-17 추가 표). 팀 표에 컬럼을 더하지 않고 이 표에만 둔다.
CREATE TABLE IF NOT EXISTS public.conversation_generations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES public.conversation_sessions(id),
    request_id text NOT NULL,
    model_id bigint NOT NULL REFERENCES public.models(id),
    user_message_id bigint NOT NULL,
    assistant_message_id bigint NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'streaming', 'completed', 'failed', 'failed_partial', 'save_failed'
    )),
    text text NOT NULL DEFAULT '',
    error_code text,
    billable boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT conversation_generations_request_unique UNIQUE (session_id, request_id),
    CONSTRAINT conversation_generations_assistant_message_unique UNIQUE (assistant_message_id),
    CONSTRAINT conversation_generations_user_message_fk
        FOREIGN KEY (session_id, user_message_id)
        REFERENCES public.conversation_messages(session_id, id),
    CONSTRAINT conversation_generations_assistant_message_fk
        FOREIGN KEY (session_id, assistant_message_id)
        REFERENCES public.conversation_messages(session_id, id)
);
-- 새 빈 표에 만드는 인덱스라 CONCURRENTLY가 필요 없다.
CREATE UNIQUE INDEX IF NOT EXISTS conversation_generations_one_active_idx
    ON public.conversation_generations(session_id) WHERE status IN ('pending', 'streaming');
CREATE INDEX IF NOT EXISTS conversation_generations_session_idx
    ON public.conversation_generations(session_id, id);
SELECT pg_temp.kan17_expect('public.conversation_generations', $exp$rel r p
col id bigint NOT NULL IDENTITY ALWAYS
col session_id bigint NOT NULL
col request_id text NOT NULL
col model_id bigint NOT NULL
col user_message_id bigint NOT NULL
col assistant_message_id bigint NOT NULL
col status text NOT NULL DEFAULT 'pending'::text
col text text NOT NULL DEFAULT ''::text
col error_code text
col billable boolean NOT NULL DEFAULT false
col created_at timestamp with time zone NOT NULL DEFAULT clock_timestamp()
col updated_at timestamp with time zone NOT NULL DEFAULT clock_timestamp()
con conversation_generations_assistant_message_fk FOREIGN KEY (session_id, assistant_message_id) REFERENCES conversation_messages(session_id, id)
con conversation_generations_assistant_message_unique UNIQUE (assistant_message_id)
con conversation_generations_model_id_fkey FOREIGN KEY (model_id) REFERENCES models(id)
con conversation_generations_pkey PRIMARY KEY (id)
con conversation_generations_request_unique UNIQUE (session_id, request_id)
con conversation_generations_session_id_fkey FOREIGN KEY (session_id) REFERENCES conversation_sessions(id)
con conversation_generations_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'streaming'::text, 'completed'::text, 'failed'::text, 'failed_partial'::text, 'save_failed'::text])))
con conversation_generations_user_message_fk FOREIGN KEY (session_id, user_message_id) REFERENCES conversation_messages(session_id, id)
idx CREATE INDEX conversation_generations_session_idx ON public.conversation_generations USING btree (session_id, id)
idx CREATE UNIQUE INDEX conversation_generations_one_active_idx ON public.conversation_generations USING btree (session_id) WHERE (status = ANY (ARRAY['pending'::text, 'streaming'::text]))$exp$);

COMMIT;
