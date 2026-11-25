-- 테스트 전용 사본: 2026-11-20 공용 DB에 적용된 팀 승인 DDL(Confluence 3047447) 객체 정의 그대로.
-- 공용 DB 상태(팀 표 있음, models 비어 있음)를 흉내 내는 데만 쓴다. 공용 DB에 다시 실행하지 않는다.
CREATE TABLE public.models (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    provider text NOT NULL,
    provider_model_id text NOT NULL
);
ALTER TABLE public.conversation_sessions
    ADD COLUMN selected_model_id bigint NOT NULL,
    ADD CONSTRAINT conversation_sessions_selected_model_fk
        FOREIGN KEY (selected_model_id) REFERENCES public.models(id);
CREATE TABLE public.conversation_messages (
    session_id bigint NOT NULL REFERENCES public.conversation_sessions(id),
    id bigint GENERATED ALWAYS AS IDENTITY,
    position bigint NOT NULL CHECK (position >= 0),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    status text NOT NULL CHECK (status IN ('pending', 'streaming', 'completed', 'failed', 'interrupted')),
    PRIMARY KEY (session_id, id),
    CONSTRAINT conversation_messages_position_unique UNIQUE (session_id, position)
);
CREATE TABLE public.conversation_message_parts (
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
