-- 출처: kickoff/creation/schema-draft.sql (원문 그대로 복사, 운영 마이그레이션 아님).
-- 로컬 개발·테스트 DB 초기화용. 원본이 바뀌면 이 파일도 같이 갱신한다.

-- PostgreSQL / CREATE-US-01~07 및 대화 세션 상태 부분 초안. 운영 마이그레이션이 아니다.
-- 확정 요구와 구현 제안을 구분한다. revision 제외. 상세: schema-design.md

-- CREATE-US-06: 업로드가 성공한 실제 저장 파일의 공통 정보. 계정 FK는 후속 연결.
-- 정지 이미지는 확인한 자르기 결과, GIF는 변경하지 않은 파일을 가리키는 구현 제안.
CREATE TABLE images (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id bigint NOT NULL,
    storage_key text NOT NULL UNIQUE,
    mime_type text NOT NULL CHECK (mime_type IN ('image/jpeg', 'image/png', 'image/webp', 'image/gif')),
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    orphaned_at timestamptz
);
-- MIME 선언만으로 실제 이미지 형식/업로드 성공/권한을 검증하지 않는다.
-- 파일당 5MB, 대상별 GIF 허용/장수, 실제 파일 검사와 자르기는 업로드/저장 서비스 책임.
-- orphaned_at은 연결 없는 상태가 시작된 시각 제안. 연결 생성 시 해제하고 마지막 해제 시 기록.
-- 미저장 업로드는 생성 시부터 미사용 후보. 유예 기간/작업 주기/동시 제어는 아직 미정.
-- FK는 사용 중인 DB 행 삭제만 막는다. 실제 파일 삭제·재확인·재시도는 별도 구현해야 한다.

CREATE TABLE stories (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id bigint NOT NULL,
    title text,
    subtitle text,
    world_description text,
    save_state text NOT NULL DEFAULT 'draft'
        CHECK (save_state IN ('draft', 'private', 'public')),
    saved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deleted_at timestamptz
);
-- owner_id는 제작자 계정 식별값. 실제 계정 FK/인증은 계정 모델 설계 후 연결한다.
-- saved_at은 서버가 저장한 시각이며 revision/충돌 키가 아니다. 자식만 바뀌어도 저장 서비스가
-- 같은 트랜잭션에서 stories를 UPDATE해야 한다. UI는 커밋 성공 응답 후에만 완료로 표시한다.
-- deleted_at은 공개 범위와 별개다. 소프트 삭제 시 자식/세션/이미지 연결을 유지한다.

-- CREATE-US-07: 허용 상태 전환과 복구/물리 삭제 금지를 DB에서도 지키는 구현 제안.
CREATE FUNCTION enforce_story_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Stories must be soft deleted and retained indefinitely'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.deleted_at IS NOT NULL THEN
        RAISE EXCEPTION 'A deleted story cannot be restored or edited'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
        RAISE EXCEPTION 'Story ownership cannot be reassigned by editing'
            USING ERRCODE = '23514';
    END IF;
    IF (OLD.save_state IN ('private', 'public') AND NEW.save_state = 'draft')
       OR (OLD.save_state = 'public' AND NEW.save_state = 'private') THEN
        RAISE EXCEPTION 'This story save-state transition is not allowed'
            USING ERRCODE = '23514';
    END IF;
    NEW.saved_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER stories_lifecycle_guard
    BEFORE UPDATE OR DELETE ON stories
    FOR EACH ROW EXECUTE FUNCTION enforce_story_lifecycle();
-- 인증 소유자 검사·삭제 최종 확인·자식 쓰기 차단은 저장/삭제 서비스 책임이다.
-- DB 관리자의 TRUNCATE/제약 제거까지 제한하는 보안 모델은 이 초안 범위가 아니다.
-- draft→draft/private/public, private→private/public, public→public만 허용한다.
-- 자동 저장 요청은 현재 draft에서만 허용. UI 선택 제한만 믿지 않고 서버에서 재확인한다.
-- 미완성·초과 초안 허용/완성 저장 검증, 요청 중복방지, 실패/재시도 UI는 서비스 책임.
-- 스토리 및 모든 변경 자식을 한 트랜잭션으로 저장하고 실패 시 전부 롤백한다.
-- 저장·삭제는 스토리 행 잠금을 공유한다. revision 비교/실시간 알림/자동 병합은 추가하지 않는다.

-- 미완성 임시 저장을 위해 제목·소개·세계관의 NULL/초과 입력을 보존한다.
-- 공개/비공개 저장 시 필수값·합산 길이를 서비스에서 검증한다.

CREATE TABLE characters (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id bigint NOT NULL REFERENCES stories(id),
    name text,
    description text,
    position integer NOT NULL CHECK (position >= 0),
    CONSTRAINT characters_story_position_unique
        UNIQUE (story_id, position) DEFERRABLE INITIALLY DEFERRED
);
-- 이름은 원문 그대로 저장하고 비교용 컬럼/인덱스는 두지 않는다.
-- 저장 처리에서 영문 대소문자와 띄어쓰기를 제외해 중복 검사한다.
-- 유저 캐릭터까지 포함한 검사는 해당 모델 설계 후 연결한다.
-- 고정 id로 수정/재배치하며 얼굴 이미지는 character_images.character_id로 연결한다.

CREATE TABLE tags (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- 제안: C 비교 기준에서 정확히 같은 표기의 중복 행 생성을 막는다.
    -- 영문 대소문자/공백까지 동일 태그로 볼지는 미결이며 확정 시 키를 수정한다.
    name text COLLATE "C" NOT NULL,
    CONSTRAINT tags_name_unique UNIQUE (name)
);

CREATE TABLE story_tags (
    story_id bigint NOT NULL REFERENCES stories(id),
    tag_id bigint NOT NULL REFERENCES tags(id),
    PRIMARY KEY (story_id, tag_id)
);
CREATE INDEX story_tags_tag_story_idx ON story_tags(tag_id, story_id);
-- 연결 테이블에는 원문 중복·순서·별도 id·미연결 행을 넣지 않는다.
-- 태그 원문은 tags.name에 보존하고 자동 trim/대소문자 변경을 하지 않는다.
-- 태그 순서는 제품 요구로 확정되지 않았으므로 보장하지 않는다.
-- 오류 상태 태그 입력의 임시 저장은 미결: 이 테이블로 지원한다고 주장하지 않는다.
-- 미검증 편집 데이터 보관 구조는 필요성이 확정된 뒤 별도로 설계한다.

-- 집계 뷰/집계 테이블은 만들지 않는다.
-- 태그 자동검색 조회에서 tags → story_tags → stories를 연결하고
-- save_state='public' AND deleted_at IS NULL인 스토리만 집계하여 사용 수 내림차순으로 반환한다.
-- 같은 스토리/태그 연결은 PK로 하나뿐이다.
-- 비공개/임시 저장에도 태그 연결은 유지하며 공개 사용 수에서만 제외한다.
-- 후보 매칭·동률 정렬·0개 태그 노출 여부는 미결이다.

-- 서비스 구현 의무 (SQL만으로 완료되지 않음):
-- 소유권 확인, 스토리 행 잠금 규약, 스토리/캐릭터/태그를 한 트랜잭션으로 저장.
-- 완성 저장: 필수값, 캐릭터 1~10명, 기본 정보+캐릭터 이름/상세 합산 3000자,
-- 태그 최대 10개/각 10자, 이름 형식/중복 검증. 실패 시 입력을 자르지 않고 보존.
-- 새 태그도 허용: 검증 후 공통 tags 재사용/생성과 연결을 같은 트랜잭션에 포함.
-- 공유 태그를 수정해 타 스토리를 변경하지 않음; 태그 변경은 연결 교체 방식 제안.
-- 다음 LLM 응답은 일관된 스냅샷으로 최신 저장 설정/캐릭터/태그를 읽음.
-- 사용자 승인: 스토리 편집은 revision 충돌 방지 없이 마지막 저장 완료 내용을 적용.
-- 계정 FK와 실제 인증/저장/미디어 서비스,
-- 글자 단위/공백 범위, 태그 식별 세부 정책은 후속 설계 대상이다.


-- CREATE-US-02: 표현 설정. 별도 1:1 테이블로 분리하는 것은 구현 제안.
CREATE TABLE story_expression_settings (
    story_id bigint PRIMARY KEY REFERENCES stories(id),
    point_of_view text NOT NULL DEFAULT 'third_person'
        CHECK (point_of_view IN ('first_person', 'second_person', 'third_person')),
    tense text NOT NULL DEFAULT 'past' CHECK (tense IN ('past', 'present')),
    composition text NOT NULL DEFAULT 'balanced'
        CHECK (composition IN ('dialogue', 'action', 'balanced')),
    response_length text NOT NULL DEFAULT 'normal'
        CHECK (response_length IN ('short', 'normal', 'long')),
    style_mode text NOT NULL DEFAULT 'none'
        CHECK (style_mode IN ('none', 'preset', 'custom')),
    -- 고정 8개 복수 선택: 별도 사전 테이블 대신 코드 배열로 저장하는 제안.
    preset_styles text[] NOT NULL DEFAULT ARRAY[]::text[],
    custom_style text,
    CONSTRAINT expression_styles_allowed CHECK (
        preset_styles <@ ARRAY['dynamic', 'lyrical', 'tense', 'humorous',
                              'plain', 'delicate', 'romantic', 'warm']::text[]
        AND array_position(preset_styles, NULL) IS NULL
        AND cardinality(preset_styles) <= 2
    ),
    CONSTRAINT expression_style_exclusive CHECK (
        (style_mode = 'none' AND cardinality(preset_styles) = 0 AND custom_style IS NULL)
        OR (style_mode = 'preset' AND custom_style IS NULL)
        OR (style_mode = 'custom' AND cardinality(preset_styles) = 0)
    )
);
-- 설정 행은 스토리 생성 시 같은 트랜잭션에서 생성하는 제안. FK만으로 존재를 강제하지는 않는다.
-- preset 배열은 집합으로 취급하며 중복 코드는 저장 서비스에서 제거한다.
-- 초과 커스텀도 임시 저장 가능하도록 1000자 CHECK는 두지 않는다.
-- preset/custom 편집 중 빈 값의 저장·완료 검증은 서비스에서 처리한다.
-- 방식 전환 시 이전 방식 내용 보존은 요구가 아니므로 비활성 값은 함께 저장하지 않는다.
-- 응답 길이는 단계만 저장한다. 목표 분량/가이드 문장은 승인된 상세를 코드 설정으로 관리하는 제안.

CREATE TABLE fewshots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id bigint NOT NULL REFERENCES stories(id),
    position integer NOT NULL CHECK (position >= 0),
    CONSTRAINT fewshots_order_unique
        UNIQUE (story_id, position) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE fewshot_messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fewshot_id bigint NOT NULL,
    speaker_kind text NOT NULL CHECK (speaker_kind IN ('user', 'character', 'narrator')),
    character_id bigint,
    body text,
    position integer NOT NULL CHECK (position >= 0),
    CONSTRAINT fewshot_message_speaker CHECK (
        (speaker_kind = 'character' AND character_id IS NOT NULL)
        OR (speaker_kind IN ('user', 'narrator') AND character_id IS NULL)
    ),
    CONSTRAINT fewshot_message_fewshot_fk
        FOREIGN KEY (fewshot_id) REFERENCES fewshots(id) ON DELETE CASCADE,
    -- 승인된 동작: 캐릭터 삭제 시 그 인물이 발화자인 말풍선만 삭제한다.
    CONSTRAINT fewshot_message_character_fk
        FOREIGN KEY (character_id) REFERENCES characters(id)
        ON DELETE CASCADE,
    CONSTRAINT fewshot_messages_order_unique
        UNIQUE (fewshot_id, position) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX fewshot_messages_character_idx
    ON fewshot_messages(character_id);
-- story_id는 fewshots를 통해 조회한다. 같은 스토리의 발화자인지는 저장 서비스에서 검사한다.
-- 검사/저장을 같은 트랜잭션에서 수행하고 기존 fewshot/character의 story_id 변경도 막는다.
-- NULL character_id인 유저/나레이터 메시지는 캐릭터 삭제 연쇄 대상이 아니다.
-- body는 Markdown과 {{user}}를 원문 보존한다. 이름표는 characters.name을 참조한다.
-- 이름 변경 시 body 자동 치환 금지. 유저/나레이터는 가짜 character 행을 생성하지 않는다.
-- 같은 발화자 연속 메시지도 각각 행으로 보존. 예시/메시지 개수 제한 없음.
-- 예시 이동은 예시 position, 말풍선 이동은 해당 예시 내부 position만 갱신한다.
-- 기존 메시지의 fewshot_id 변경은 저장 API에서 거절해야 한다(DDL 단독으로 금지되지 않음).
-- 본문 누락/빈 예시의 완료 저장 정책은 미결: 빈 body/빈 예시를 저장 구조로 임의 금지하지 않는다.
-- 승인된 묶음 삭제: fewshot 삭제 시 소속 메시지도 삭제하고 다른 예시/메시지는 유지한다.
-- 삭제/추가/본문 수정 후 남은 원문 길이를 재계산하고 순서값의 빈 구간은 허용한다.
-- 전체 body 원문 합산 초기 2000자, 커스텀 초기 1000자: 서비스 설정값으로 검증.
-- 공백/줄바꿈/서식/치환 표기 포함. 기본 정보 3000자와 별개. 입력 절삭 금지.
-- 초과 임시 저장 허용, 공개/비공개 저장 거절. 이름표/메타데이터 계산 범위는 후속 확인.
-- 프롬프트: 예시 position → 메시지 position 순서, 관리 ID/position 제외,
-- 승인된 {{user}} 치환 외 요약/재작성/본문 인물명 자동 변경 없이 전달한다.


-- CREATE-US-03: 인트로. 퓨샷과 유사한 구조이나 목적/필수값/길이 범위가 다르므로 분리 제안.
CREATE TABLE intros (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id bigint NOT NULL REFERENCES stories(id),
    title text,
    position integer NOT NULL CHECK (position >= 0),
    CONSTRAINT intros_order_unique
        UNIQUE (story_id, position) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE intro_messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    intro_id bigint NOT NULL REFERENCES intros(id) ON DELETE CASCADE,
    speaker_kind text NOT NULL CHECK (speaker_kind IN ('user', 'character', 'narrator')),
    character_id bigint REFERENCES characters(id) ON DELETE CASCADE,
    body text,
    position integer NOT NULL CHECK (position >= 0),
    CONSTRAINT intro_message_speaker CHECK (
        (speaker_kind = 'character' AND character_id IS NOT NULL)
        OR (speaker_kind IN ('user', 'narrator') AND character_id IS NULL)
    ),
    CONSTRAINT intro_messages_order_unique
        UNIQUE (intro_id, position) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX intro_messages_character_idx ON intro_messages(character_id);
-- intro_messages에는 story_id를 중복 저장하지 않고 intros를 통해 조회한다.
-- 발화 캐릭터와 인트로가 같은 스토리인지는 저장 서비스에서 검사한다.
-- 기본 인트로는 현재 position이 가장 작은 행이다. is_default/default_intro_id는 두지 않는다.
-- 순서가 0부터 연속일 필요는 없다. 재배치/삭제 후에도 첫 행을 조회해 기본을 결정한다.
-- 첫 인트로/마지막 인트로 삭제 허용. 0개이면 기본 없음, 임시 저장만 허용한다.
-- 완성 저장 서비스 검증: 스토리당 1~5개, 각 title 필수/최대 30자, 본문 필수.
-- 본문은 인트로별 body 원문 합계 초기 2000자(제목 제외). 전체 인트로 합산 한도 없음.
-- 본문 없음/2000자 초과도 임시 저장 가능하도록 DB NOT NULL/길이 CHECK를 두지 않는다.
-- 개수 최대/제목 검증의 임시 저장 세부 정책은 7번과 함께 확인한다.
-- 본문 변경/메시지 삭제/묶음 삭제/캐릭터 연쇄 삭제 후 인트로별 원문 길이 재계산.
-- 정렬/치환/이름표/Markdown 규칙은 퓨샷과 동일하며 원문을 요약/재작성하지 않는다.
-- 기존 메시지의 intro_id 변경은 저장 서비스에서 거절(다른 인트로로 이동 금지).
-- @ 발화자 선택은 UI 입력 방식이며 body에 이름을 자동 삽입하거나 별도 mention 행을 만들지 않는다.
-- 조회/저장 시 양쪽의 story_id 변경 금지·소유권·동일 스토리 검사/쓰기를 같은 트랜잭션에서 처리.
-- 이용자의 선택 intro_id/미리보기/대화 이력 반영/이어하기는 CHAT 설계에서 별도로 다룬다.


-- CREATE-US-04 / CHAT-US-01·07·10: 제작 초기값과 세션 현재값 분리.
-- 아래 테이블·컬럼·키는 구현 초안이다. 운영 DB에 적용한 마이그레이션이 아니다.
-- 사용자/계정 스키마, 메시지, 버전 이력, 세션 삭제 UI는 이번에 확정하지 않는다.

-- 1. 인트로별 장면 초기값. 날짜·시간 원문은 부분 입력 임시 저장을 위해 text.
CREATE TABLE intro_scene_settings (
    intro_id bigint PRIMARY KEY REFERENCES intros(id) ON DELETE CASCADE,
    date_mode text CHECK (date_mode IN ('calendar', 'world', 'elapsed')),
    era text,
    year_input text,
    month_input text,
    day_input text,
    reference_event text,
    elapsed_day_input text,
    time_format text NOT NULL DEFAULT '24h' CHECK (time_format IN ('24h', '12h')),
    hour_input text,
    minute_input text,
    meridiem text CHECK (meridiem IN ('am', 'pm')),
    place text
);
-- 해당 모드의 입력만 저장한다. 방식 전환 시 기존 날짜/시각 입력을 지우는 것은 저장 서비스 책임.
-- 모든 날짜/시각 원문 NULL은 미지정. CURRENT_DATE/CURRENT_TIME 자동 기본값을 두지 않는다.
-- 연 1~9999, 월/일 공통 규칙(2월 28일 고정), 연호/사건명 10자, 장소 30자와
-- 부분 입력의 완성 저장 제한은 save_state에 따라 서비스에서 검증한다.
-- 초과·부분 입력 임시 저장을 막는 varchar 길이/숫자 범위 CHECK를 원문에 두지 않는다.

CREATE TABLE intro_character_states (
    intro_id bigint NOT NULL REFERENCES intros(id) ON DELETE CASCADE,
    character_id bigint NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    clothing text,
    inner_thought text,
    PRIMARY KEY (intro_id, character_id)
);
-- story_id는 intro/character에서 구하며 동일 스토리 검사를 같은 저장 트랜잭션에서 수행한다.
-- 등장 캐릭터만 연결한다. 유저 캐릭터의 착장/속마음을 위한 가짜 characters 행은 만들지 않는다.
-- 행이 없거나 필드 NULL이면 미지정. 착장 300자·속마음 100자는 완성 저장 서비스 검사.

-- 2. 인물 사이 방향별 초기 호감도/관계. 인물 연결이 주체이며 story_id/intro_id를 중복 저장하지 않는다.
-- 스토리 공통은 모든 인트로가 같은 시작값을 쓴다는 적용 범위이지 스토리의 단일 속성이라는 뜻이 아니다.
CREATE TABLE character_initial_affinities (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_character_id bigint NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    target_kind text NOT NULL CHECK (target_kind IN ('character', 'user')),
    target_character_id bigint REFERENCES characters(id) ON DELETE CASCADE,
    affinity_input text DEFAULT '0',
    CONSTRAINT initial_affinity_target CHECK (
        (target_kind = 'character' AND target_character_id IS NOT NULL)
        OR (target_kind = 'user' AND target_character_id IS NULL)
    ),
    CONSTRAINT initial_affinity_direction_unique UNIQUE NULLS NOT DISTINCT
        (source_character_id, target_kind, target_character_id)
);
-- 캐릭터 하나의 단일 값이 아니라 출발 캐릭터 → 상대별 독립 값이다.
-- A→user, A→B, B→A를 별도 행으로 저장하며 역방향을 같은 값으로 자동 변경하지 않는다.
-- 기본값 0, -100~100 정수의 완료 검증 및 지운 값/범위 초과 임시 저장 정책 유지.
-- 스토리는 source_character_id → characters.story_id로 구한다. target도 같은 소속인지 서비스가 검사한다.
-- 모든 인물 조합 행의 사전 생성·미작성 쌍의 의미·유저를 출발점으로 하는 값은 미결.

CREATE TABLE character_initial_relationships (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_kind text NOT NULL CHECK (source_kind IN ('character', 'user')),
    source_character_id bigint REFERENCES characters(id) ON DELETE CASCADE,
    target_kind text NOT NULL CHECK (target_kind IN ('character', 'user')),
    target_character_id bigint REFERENCES characters(id) ON DELETE CASCADE,
    label text,
    CONSTRAINT initial_relationship_source CHECK (
        (source_kind = 'character' AND source_character_id IS NOT NULL)
        OR (source_kind = 'user' AND source_character_id IS NULL)
    ),
    CONSTRAINT initial_relationship_target CHECK (
        (target_kind = 'character' AND target_character_id IS NOT NULL)
        OR (target_kind = 'user' AND target_character_id IS NULL)
    ),
    CONSTRAINT initial_relationship_not_user_pair CHECK (
        source_kind <> 'user' OR target_kind <> 'user'
    ),
    CONSTRAINT initial_relationship_direction_unique UNIQUE NULLS NOT DISTINCT
        (source_kind, source_character_id, target_kind, target_character_id)
);
-- NULLS NOT DISTINCT는 PostgreSQL 15+ 문법. user는 세션의 극 중 유저 자리이며 계정 ID가 아니다.
-- 관계 미지정은 행 없음으로 표현한다. 관계명 초과 원문/작성 중 빈 값도 보존할 수 있다.
-- 스토리는 연결된 캐릭터에서 구한다. source가 user이면 target_character_id를 통해 구한다.
-- 양쪽이 character이면 같은 스토리인지 저장 서비스에서 검사하며 기존 소속 변경을 막는다.
-- 역방향은 독립 행이다. 한쪽 저장으로 반대쪽 행을 자동 생성하지 않는다.

-- 3. 대화 세션. 스토리/선택 인트로와 연결하되 현재 상태는 세션의 별도 행에 저장한다.
-- 사용자 확인: 인트로별 장면과 스토리 공통 호감도/관계는 세션 시작 시 하나의 논리 상태로 합친다.
-- 테이블을 나눈 것은 저장 구조이며 초기 출처를 런타임의 두 상태 권한으로 유지하지 않는다.
-- 대화 중에는 세션 현재값을 갱신하고 다른 세션/제작 초기값에 쓰거나 매번 초기화하지 않는다.
-- 인계 문서: ../design/conversation-state-handoff.md. 생성 4번은 입력/검증/저장/복원만 검수한다.
CREATE TABLE conversation_sessions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id bigint NOT NULL,
    story_id bigint NOT NULL REFERENCES stories(id),
    source_intro_id bigint REFERENCES intros(id) ON DELETE SET NULL
);
-- owner_id의 계정 FK는 계정 모델 설계 후 연결한다. 이것만으로 인증/권한 완료가 아니다.
-- source_intro_id는 시작 출처 추적용. 현재 상태를 얻기 위해 초기값을 매번 조인하지 않는다.
-- 인트로 삭제가 기존 세션 현재값을 삭제하지 않게 SET NULL을 제안했다.
-- story_id와 source_intro_id의 같은 스토리 여부·접근 권한은 생성 트랜잭션에서 검증한다.

CREATE TABLE conversation_scene_states (
    session_id bigint PRIMARY KEY REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    date_mode text CHECK (date_mode IN ('calendar', 'world', 'elapsed')),
    era text,
    year integer,
    month integer,
    day integer,
    reference_event text,
    elapsed_day integer,
    -- 런타임 시각은 24시간제로 정규화하는 구현 제안. 제작자의 입력 체계와 분리한다.
    hour integer,
    minute integer,
    place text
);
-- 세션 생성 때 검증된 인트로 초기값을 이 행으로 복사/해석하여 저장한다.
-- 미지정은 NULL 그대로다. 생성 시 일차 상한 9999를 런타임 진행 상한으로 옮기지 않는다.
-- 현재값의 변화/날짜 넘김/사건 전환 규칙은 대화 설계 대상이며 이 DDL로 확정하지 않는다.
-- PK는 세션당 최대 한 행을 보장한다. 필수 현재 상태 행 생성은 세션 생성 트랜잭션 책임이다.

CREATE TABLE conversation_character_states (
    session_id bigint NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    id bigint GENERATED ALWAYS AS IDENTITY,
    source_character_id bigint REFERENCES characters(id) ON DELETE SET NULL,
    name_snapshot text,
    clothing text,
    inner_thought text,
    PRIMARY KEY (session_id, id),
    CONSTRAINT session_character_source_unique UNIQUE (session_id, source_character_id)
);
-- 이름/세션 내 ID를 보존하여 제작 캐릭터 삭제가 세션 기록을 연쇄 삭제하지 않는 초안이다.
-- 원본 연결이 NULL이어도 세션 내 ID는 남는다. 기존 세션의 삭제 캐릭터 취급 세부는 미결이다.
-- 선택 인트로의 착장/속마음을 복사한다. 호감도는 conversation_affinities에서 방향별로 관리한다.
-- 최신 원본을 조인해 매번 초기값으로 덮어쓰거나 새 세션에 다른 세션의 변화를 복사하지 않는다.
-- 캐릭터가 해당 세션의 story_id 소속인지 서비스에서 검사한다.

CREATE TABLE conversation_relationships (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    source_kind text NOT NULL CHECK (source_kind IN ('character', 'user')),
    source_session_character_id bigint,
    target_kind text NOT NULL CHECK (target_kind IN ('character', 'user')),
    target_session_character_id bigint,
    label text,
    CONSTRAINT session_relationship_source CHECK (
        (source_kind = 'character' AND source_session_character_id IS NOT NULL)
        OR (source_kind = 'user' AND source_session_character_id IS NULL)
    ),
    CONSTRAINT session_relationship_target CHECK (
        (target_kind = 'character' AND target_session_character_id IS NOT NULL)
        OR (target_kind = 'user' AND target_session_character_id IS NULL)
    ),
    CONSTRAINT session_relationship_not_user_pair CHECK (
        source_kind <> 'user' OR target_kind <> 'user'
    ),
    CONSTRAINT session_relationship_source_fk
        FOREIGN KEY (session_id, source_session_character_id)
        REFERENCES conversation_character_states(session_id, id) ON DELETE CASCADE,
    CONSTRAINT session_relationship_target_fk
        FOREIGN KEY (session_id, target_session_character_id)
        REFERENCES conversation_character_states(session_id, id) ON DELETE CASCADE,
    CONSTRAINT session_relationship_direction_unique UNIQUE NULLS NOT DISTINCT
        (session_id, source_kind, source_session_character_id, target_kind, target_session_character_id)
);
-- 같은 세션 인물만 관계에 연결하는 복합 FK다. 다른 세션의 인물 ID는 거절한다.
-- 스토리 공통 관계는 시작 시 해당 세션의 인물 ID로 대응시켜 복사한다.
-- 세션 삭제 API/UI는 미설계다. CASCADE는 부모 물리 삭제 시 자식 행 정합성만 뜻한다.
-- 상태 이력·재생성/삭제 시 되돌림·LLM 판정·업데이트 충돌·기존 세션 설정 변경 반영은 후속 설계다.


-- 세션의 상대별 현재 호감도. 캐릭터 상태 한 행의 단일 숫자에서 분리한다.
CREATE TABLE conversation_affinities (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    source_session_character_id bigint NOT NULL,
    target_kind text NOT NULL CHECK (target_kind IN ('character', 'user')),
    target_session_character_id bigint,
    affinity integer,
    CONSTRAINT session_affinity_target CHECK (
        (target_kind = 'character' AND target_session_character_id IS NOT NULL)
        OR (target_kind = 'user' AND target_session_character_id IS NULL)
    ),
    CONSTRAINT session_affinity_source_fk
        FOREIGN KEY (session_id, source_session_character_id)
        REFERENCES conversation_character_states(session_id, id) ON DELETE CASCADE,
    CONSTRAINT session_affinity_target_fk
        FOREIGN KEY (session_id, target_session_character_id)
        REFERENCES conversation_character_states(session_id, id) ON DELETE CASCADE,
    CONSTRAINT session_affinity_direction_unique UNIQUE NULLS NOT DISTINCT
        (session_id, source_session_character_id, target_kind, target_session_character_id)
);
-- 초기 호감도 방향별 행을 세션 인물 ID로 대응시켜 복사한다. 현재값은 세션별로 갱신한다.
-- 관계명과 호감도는 독립: 한쪽이 없다는 이유로 다른 쪽 입력을 강제하지 않는다.
-- 런타임 증감/범위 보정은 대화 설계에서 결정하며 생성 범위를 고정 CHECK로 이식하지 않는다.


-- CREATE-US-05: 제작자 프리셋 / 이용자 공통 원본 / 세션 적용값을 분리한다.
-- 단일 이미지는 images.id FK. 프리셋 초안의 이미지 누락은 NULL로 보존한다.
-- 개인 원본/세션 적용값의 필수 이미지는 NOT NULL. 실제 파일·권한 검증은 별도다.
CREATE TABLE story_user_presets (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id bigint NOT NULL REFERENCES stories(id),
    name text,
    image_id bigint REFERENCES images(id),
    description text,
    position integer NOT NULL CHECK (position >= 0),
    CONSTRAINT story_user_presets_order_unique
        UNIQUE (story_id, position) DEFERRABLE INITIALLY DEFERRED
);
-- 이름/이미지 누락·오류·합계 초과 초안 보존을 위해 NULL과 긴 text를 허용한다.
-- 완성 저장: 0~5개, 제공한 각 프리셋의 이름/이미지 1장 필수, 상세 선택,
-- 이름+상세 원문 초기 1000자. {{user}}는 치환하지 않고 이름 원문으로 보존한다.
-- 여러 {{user}} 프리셋 허용. 직접 이름 중복은 같은 스토리의 캐릭터/다른 직접 이름과 검사한다.
-- 스토리 공개 상태와 같은 저장 트랜잭션에서 검증한다. 이름 비교키 UNIQUE로 초안을 막지 않는다.
-- position은 편집 순서만 의미한다. 첫 프리셋 자동 선택은 결정하지 않는다.

CREATE TABLE user_play_settings (
    owner_id bigint PRIMARY KEY,
    default_play_name text
);
-- 계정 닉네임과 별개인 기본 플레이 이름. 실제 계정 FK는 계정 모델 설계 후 연결한다.
-- 위치/등록 흐름/초기 기본 이름은 미정. 미설정 값을 닉네임으로 자동 보정하지 않는다.

CREATE TABLE user_profiles (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    image_id bigint NOT NULL REFERENCES images(id),
    description text
);
-- 이용자 개인 원본: 특정 story_id/save_state/목록 기본 선택/개수 제한/이름 UNIQUE를 두지 않는다.
-- 이름·이미지 필수, 상세 선택. 원문 형식/이미지 유효성/프로필별 합산 1000자는 저장 서비스 책임.
-- 개인 프로필 임시 저장 정책은 확정하지 않았다. 제작자의 draft/private/public을 가져오지 않는다.
-- owner_id 계정 FK는 계정 설계 후 연결. 서버는 인증 소유자와 일치하는 원본만 선택/수정하게 한다.
CREATE INDEX user_profiles_owner_idx ON user_profiles(owner_id);

CREATE TABLE conversation_user_profiles (
    session_id bigint PRIMARY KEY REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    source_kind text NOT NULL CHECK (source_kind IN ('preset', 'personal')),
    source_preset_id bigint REFERENCES story_user_presets(id) ON DELETE SET NULL,
    source_profile_id bigint REFERENCES user_profiles(id) ON DELETE SET NULL,
    name text NOT NULL,
    image_id bigint NOT NULL REFERENCES images(id),
    description text,
    CONSTRAINT applied_user_profile_source_exclusive CHECK (
        (source_kind = 'preset' AND source_profile_id IS NULL)
        OR (source_kind = 'personal' AND source_preset_id IS NULL)
    )
);
-- 세션과 필수 1:1. 아래 지연 FK가 커밋 시 적용 프로필의 존재까지 보장한다.
-- 선택 시 해당 출처 ID는 필수. 원본 삭제 후에도 적용값 보존을 위해 NULL을 허용하는 기술 제안.
-- name은 적용한 극 중 이름. preset.name={{user}}이면 default_play_name을 쓰고 직접 이름은 유지한다.
-- 적용 전에 스토리의 등장 캐릭터 이름과 충돌 검사, 실패하면 기존 적용값을 바꾸지 않는다.
-- 같은 session_id의 user 관계/호감도는 이 적용 역할을 가리킨다. 원본 profile_id에 직접 묶지 않는다.
-- 세션 내 이름/이미지/상세 편집은 이 행만 변경하고 원본/다른 세션에 되쓰지 않는다.
-- 본문 {{user}}는 이 최종 이름으로 표시/프롬프트 때 치환하며 제작 원문을 덮어쓰지 않는다.
-- preset 동일 story, personal 동일 owner, 이름/이미지/1000자 및 배타 적용은 선택 서비스가 검증한다.
-- 교체/편집의 응답 반영 시점, 과거 메시지, 기억/상태/호감도/관계 초기화는 미결이다.


-- 세션에 적용 유저 캐릭터가 반드시 하나 있어야 한다.
-- 부모를 먼저 넣고 같은 트랜잭션에서 프로필을 넣을 수 있도록 존재 검사는 커밋 때 수행한다.
ALTER TABLE conversation_sessions
    ADD CONSTRAINT conversation_sessions_user_profile_required_fk
    FOREIGN KEY (id) REFERENCES conversation_user_profiles(session_id)
    DEFERRABLE INITIALLY DEFERRED;

-- 새 적용값은 반드시 두 원본 중 하나에서 출발한다.
-- 원본 삭제 시 SET NULL로 남는 기존 적용값과, 처음부터 출처 없는 생성을 구분한다.
CREATE FUNCTION require_applied_user_profile_source()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.source_kind = 'preset' AND NEW.source_preset_id IS NULL)
       OR (NEW.source_kind = 'personal' AND NEW.source_profile_id IS NULL) THEN
        RAISE EXCEPTION 'A newly applied user profile requires its selected source'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER conversation_user_profiles_require_source
    BEFORE INSERT ON conversation_user_profiles
    FOR EACH ROW EXECUTE FUNCTION require_applied_user_profile_source();
-- source_kind별 다른 원본 FK 금지는 기존 CHECK, 실제 원본 존재는 FK가 보장한다.
-- 생성: BEGIN → 선택 원본 검증 → 세션 INSERT → 선택 원본의 적용값 INSERT → COMMIT.
-- 세션 단독 커밋/적용값 단독 삭제는 거절되며 실패 시 트랜잭션 전체를 롤백한다.
-- 프로필 교체의 UPDATE는 선택 서비스에서 새 출처/권한/이름을 검증한 뒤 수행한다.
-- 원본 삭제 이후 출처 NULL 허용은 세션의 적용 유저 캐릭터가 없어지는 것과 다르다.


-- CREATE-US-06: 목록형 이미지 연결. 첫 행(min position)이 대표이며 별도 대표 플래그 없음.
CREATE TABLE story_images (
    story_id bigint NOT NULL REFERENCES stories(id),
    image_id bigint NOT NULL REFERENCES images(id),
    position integer NOT NULL CHECK (position >= 0),
    PRIMARY KEY (story_id, image_id),
    CONSTRAINT story_images_order_unique
        UNIQUE (story_id, position) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE character_images (
    character_id bigint NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    image_id bigint NOT NULL REFERENCES images(id),
    position integer NOT NULL CHECK (position >= 0),
    PRIMARY KEY (character_id, image_id),
    CONSTRAINT character_images_order_unique
        UNIQUE (character_id, position) DEFERRABLE INITIALLY DEFERRED
);
-- 순서 교환은 같은 저장 트랜잭션에서 처리한다.
-- 스토리 0~10장, 캐릭터 완성 저장 1~5장. 초안은 마지막 삭제/0개 저장 가능.
-- 교체는 같은 position의 image_id만 변경. 실패 시 기존 연결을 유지한다.
-- 새 자르기 결과는 새 파일/이미지 행으로 등록하고 기존 파일을 덮어쓰지 않는 제안.
-- 캐릭터 삭제 CASCADE는 연결만 제거하며 images나 다른 대상/세션 연결을 삭제하지 않는다.
-- 스토리 표지 부재: characters.position 첫 인물의 character_images.position 첫 이미지 조회.
-- 이 대체 이미지를 story_images에 복제 연결하거나 3:4로 다시 자르지 않는다.

-- 세션에서 실제 사용한 파일의 보존 연결을 위한 최소 구현 제안(표시용 갤러리 아님).
CREATE TABLE conversation_image_references (
    session_id bigint NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    image_id bigint NOT NULL REFERENCES images(id),
    PRIMARY KEY (session_id, image_id)
);
-- 세션 유저 사진은 conversation_user_profiles.image_id가 직접 참조하므로 중복 연결 불필요.
-- 그 외 세션이 계속 사용하는 스토리/캐릭터 파일은 실제 사용 확정 트랜잭션에서 연결한다.
-- 복사 시점·최신 캐릭터 설정 적용·과거 표시 구조를 이 보존용 테이블로 확정하지 않는다.
-- 사용 검사 대상: story_images, character_images, story_user_presets.image_id,
-- user_profiles.image_id, conversation_user_profiles.image_id, conversation_image_references.
-- 소프트 삭제된 원본의 남아 있는 연결과 오래 열지 않은 세션도 사용 중이다.
-- 파일 정리: 유예 → 최종 무참조 검사 → 동시 연결 방지 → 실제 파일 삭제 → 성공 기록.
-- 저장 중 업로드 보호/저장소 삭제 실패 재시도/잠금 또는 작업 상태 설계는 미구현이다.

