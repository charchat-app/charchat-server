-- CREATE-US-08-AC09 검증된 사유 대상 컬럼. 저장소 안의 초안이며 원격·운영 DB에 적용하지 않았다.
-- 대상은 원문·이름 없이 `{"v","ticket","reason","content","targets"}` 문서(사유 코드·종류·명단 인물
-- ID, 그 결과를 쓴 요청 표·사유·정본 내용 해시)만 담는다. 크기는 16384바이트 이하다.
--
-- 적용 순서(정방향):
-- 1. 등급 컬럼(create_us08_story_rating.sql)을 적용한 DB에 이 파일을 적용한다. saved_at·지문
--    마이그레이션과는 순서 무관하고 재실행해도 같은 결과다. 세 파일은 같은 함수 본문
--    (app/db/schema.sql의 enforce_story_lifecycle()과 같음)을 쓴다. 본문은 컬럼 이름을 jsonb 키로만
--    보므로 대상 컬럼이 없어도 동작하고, 대상만 바뀐 UPDATE는 saved_at을 움직이지 않는다.
-- 2. 이전 버전 API(대상을 모름)는 적용 뒤에도 계속 돌 수 있다. 그 작성기는 대상을 쓰거나 비우지 않지만
--    새 검토 요청마다 요청 표를 새로 쓰므로, 남은 대상은 조회 때 요청 표·사유·정본 내용 해시가 맞지
--    않아 내보내지 않는다(사유만 나간다).
-- 3. 그 뒤에 대상을 쓰는 API를 배포한다.
--
-- 롤백(역방향, 필요 시 사유 대상 기록만 사라짐):
-- 1. 먼저 API를 대상을 모르는 이전 버전으로 되돌린다. 대상을 아는 API가 도는 동안 컬럼을 지우면
--    검토 결과 쓰기·조회가 실패한다.
-- 2. `ALTER TABLE public.stories DROP CONSTRAINT IF EXISTS stories_rating_review_targets_check,
--    DROP COLUMN IF EXISTS rating_review_targets;`를 실행한다. 함수는 그대로 둬도 된다(대상 키가
--    없으면 아무것도 하지 않는다).
--
-- 이미 있는 컬럼·제약의 정의가 다르거나 stories_lifecycle_guard 트리거가 기대한 정의가 아니면 바꾸지
-- 않고 실패한다(한 트랜잭션이라 모두 되돌아간다).
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

LOCK TABLE public.stories IN ACCESS EXCLUSIVE MODE;

ALTER TABLE public.stories ADD COLUMN IF NOT EXISTS rating_review_targets jsonb;

-- 기대 제약 정의는 같은 식으로 만든 임시 표에서 얻는다(서버 버전별 출력 형식 차이를 피한다).
CREATE TEMP TABLE us08_review_targets_expected (
    rating_review_targets jsonb,
    CONSTRAINT stories_rating_review_targets_check
        CHECK (jsonb_typeof(rating_review_targets) = 'object'
               AND octet_length(rating_review_targets::text) <= 16384)
) ON COMMIT DROP;

DO $us08$
DECLARE
    tbl CONSTANT regclass := 'public.stories'::regclass;
    expected text;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO STRICT expected FROM pg_constraint c
    WHERE c.conrelid = 'us08_review_targets_expected'::regclass
      AND c.conname = 'stories_rating_review_targets_check';
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = tbl AND a.attname = 'rating_review_targets' AND NOT a.attisdropped
          AND a.atttypid = 'jsonb'::regtype AND NOT a.attnotnull
          AND a.attidentity = '' AND a.attgenerated = '' AND d.adbin IS NULL
    ) THEN
        RAISE EXCEPTION 'CREATE-US-08: unexpected stories.rating_review_targets column definition';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = tbl AND conname = 'stories_rating_review_targets_check'
    ) THEN
        ALTER TABLE public.stories ADD CONSTRAINT stories_rating_review_targets_check
            CHECK (jsonb_typeof(rating_review_targets) = 'object'
                   AND octet_length(rating_review_targets::text) <= 16384);
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = tbl AND c.conname = 'stories_rating_review_targets_check'
          AND c.contype = 'c' AND c.convalidated AND NOT c.connoinherit
          AND pg_get_constraintdef(c.oid) = expected
    ) THEN
        RAISE EXCEPTION 'CREATE-US-08: unexpected stories_rating_review_targets_check definition';
    END IF;
END;
$us08$;

CREATE OR REPLACE FUNCTION public.enforce_story_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    review_keys CONSTANT text[] := ARRAY[
        'effective_rating', 'rating_review_status', 'rating_review_reason',
        'rating_requested_at', 'rating_pass_digest', 'rating_review_targets', 'saved_at'];
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
    -- CREATE-US-08-AC14: 통과 지문을 모르는 이전 버전 작성기가 검토 중을 부적격·판단 불가 완료
    -- (적용 등급 SAFE)로 바꾸면서 지문을 그대로 두면, 같은 내용의 거부 뒤에도 통과가 남을 수 있다.
    -- 지문을 아는 작성기는 같은 트랜잭션에 charchat.rating_pass_digest_writer='v1'을 둔다.
    -- 그 표시 없는 그런 전환은 지문을 비운다(재사용하지 않고 다시 평가한다).
    IF to_jsonb(OLD) ->> 'rating_review_status' = 'reviewing'
       AND to_jsonb(NEW) ->> 'rating_review_status' = 'completed'
       AND to_jsonb(NEW) ->> 'effective_rating' = 'SAFE'
       AND to_jsonb(NEW) ->> 'rating_pass_digest' IS NOT NULL
       AND to_jsonb(NEW) -> 'rating_pass_digest' = to_jsonb(OLD) -> 'rating_pass_digest'
       AND current_setting('charchat.rating_pass_digest_writer', true)
           IS DISTINCT FROM 'v1' THEN
        NEW := jsonb_populate_record(NEW, '{"rating_pass_digest": null}'::jsonb);
    END IF;
    IF to_jsonb(NEW) - review_keys = to_jsonb(OLD) - review_keys
       AND to_jsonb(NEW) - 'saved_at' IS DISTINCT FROM to_jsonb(OLD) - 'saved_at' THEN
        NEW.saved_at := OLD.saved_at;
    ELSE
        NEW.saved_at := clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;


DO $us08$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger t
        WHERE t.tgrelid = 'public.stories'::regclass
          AND t.tgname = 'stories_lifecycle_guard'
          AND NOT t.tgisinternal
          AND t.tgenabled = 'O'
          AND t.tgfoid = 'public.enforce_story_lifecycle()'::regprocedure
          -- 행 단위(1)·BEFORE(2)·DELETE(8)·UPDATE(16)만, 컬럼 목록·WHEN 조건 없음.
          AND t.tgtype = 27
          AND t.tgattr = ''::int2vector
          AND t.tgqual IS NULL
    ) THEN
        RAISE EXCEPTION 'CREATE-US-08: unexpected stories_lifecycle_guard trigger definition';
    END IF;
END;
$us08$;

COMMIT;
