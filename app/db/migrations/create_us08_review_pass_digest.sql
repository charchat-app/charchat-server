-- CREATE-US-08-AC14 영속 마지막 통과 지문 컬럼. 저장소 안의 초안이며 원격·운영 DB에 적용하지 않았다.
-- 지문은 원문 없이 `v<개정>:<평가기 이름공간>:<SHA-256>`만 담는다.
--
-- 적용 순서(정방향):
-- 1. 이 파일, 등급 컬럼(create_us08_story_rating.sql), saved_at(create_us08_review_saved_at.sql)
--    마이그레이션을 모두 적용한다. 세 파일은 서로 순서 무관하고 재실행해도 같은 결과다. 이 파일과
--    saved_at 파일은 같은 함수 본문(app/db/schema.sql의 enforce_story_lifecycle()과 같음)을 쓰고,
--    본문은 컬럼 이름을 jsonb 키로만 보므로 등급·지문 컬럼이 아직 없어도 동작한다. 이 파일은
--    컬럼·제약과 함수를 한 트랜잭션에서 맞추므로 지문 컬럼이 보이는 순간 함수도 맞춰져 있다.
--    사유 대상 마이그레이션(create_us08_review_targets.sql)도 같은 본문을 쓰며 순서 무관하다.
-- 2. 이전 버전 API(지문을 모름)는 적용 뒤에도 계속 돌 수 있다. 그 작성기가 검토 중을 거부(SAFE·완료)로
--    바꾸며 지문을 그대로 두면 함수가 지문을 비운다(지문을 아는 작성기는 트랜잭션 표시
--    charchat.rating_pass_digest_writer='v1'을 둔다). 이전 버전이 남긴 거부 뒤에 같은 내용이
--    재사용되지 않는다.
-- 3. 그 뒤에 지문을 쓰는 API를 배포한다. 재사용은 조정기에 이름공간을 명시적으로 줄 때만 켜지며 운영
--    앱 기본값은 꺼짐이다.
--
-- 롤백(역방향, 필요 시 재사용 기록만 사라짐):
-- 1. 먼저 API를 지문을 모르는 이전 버전으로 되돌린다(이름공간만 끄는 것으로는 부족하다: 지문을 아는
--    API는 이름공간이 없어도 검토 결과를 쓸 때 지문 컬럼을 읽고 쓴다). 그 API가 도는 동안 컬럼을
--    지우면 검토 결과 쓰기(재사용 시 확정 저장도)가 실패한다.
-- 2. 아래 제약과 컬럼을 DROP한다. 함수는 그대로 둬도 된다(지문 키가 없으면 지문 방어는 아무것도 하지
--    않고 saved_at 동작은 같다).
-- 3. saved_at 함수를 수정 전 본문으로 되돌리거나 등급 컬럼을 지우는 롤백은 2 뒤에 한다. 지문 컬럼이
--    남은 채 수정 전 함수로 되돌리면 지문 쓰기가 saved_at을 움직이고 이전 버전 거부 방어도 사라진다.
--
-- 이미 있는 컬럼·제약의 정의가 다르거나 stories_lifecycle_guard 트리거가 기대한 정의가 아니면 바꾸지
-- 않고 실패한다(한 트랜잭션이라 모두 되돌아간다).
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

LOCK TABLE public.stories IN ACCESS EXCLUSIVE MODE;

ALTER TABLE public.stories ADD COLUMN IF NOT EXISTS rating_pass_digest text;

DO $us08$
DECLARE
    tbl CONSTANT regclass := 'public.stories'::regclass;
    expected CONSTANT text :=
        'CHECK ((rating_pass_digest ~ ''^v[0-9]{1,9}:[a-z0-9][a-z0-9._-]{0,63}:[0-9a-f]{64}$''::text))';
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = tbl AND a.attname = 'rating_pass_digest' AND NOT a.attisdropped
          AND a.atttypid = 'text'::regtype AND NOT a.attnotnull
          AND a.attidentity = '' AND a.attgenerated = '' AND d.adbin IS NULL
    ) THEN
        RAISE EXCEPTION 'CREATE-US-08: unexpected stories.rating_pass_digest column definition';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = tbl AND conname = 'stories_rating_pass_digest_check'
    ) THEN
        ALTER TABLE public.stories ADD CONSTRAINT stories_rating_pass_digest_check
            CHECK (rating_pass_digest ~ '^v[0-9]{1,9}:[a-z0-9][a-z0-9._-]{0,63}:[0-9a-f]{64}$');
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = tbl AND c.conname = 'stories_rating_pass_digest_check'
          AND c.contype = 'c' AND c.convalidated AND NOT c.connoinherit
          AND pg_get_constraintdef(c.oid) = expected
    ) THEN
        RAISE EXCEPTION 'CREATE-US-08: unexpected stories_rating_pass_digest_check definition';
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
    -- saved_at 보존 예외는 아래 순서로 판정한다(위 지문 정리 뒤의 NEW 기준). 각 예외는 그 UPDATE가
    -- 해당 컬럼만 바꿨을 때만 적용하며, 두 종류가 한 UPDATE에 섞이면 saved_at을 새로 쓴다.
    -- 1) CREATE-US-09: 카테고리 분류 대상 표시(category_classification_needed)만 바뀐 UPDATE.
    -- 2) CREATE-US-08: 검토 메타데이터(review_keys)만 실제로 바뀐 UPDATE.
    IF (to_jsonb(NEW) -> 'category_classification_needed')
           IS DISTINCT FROM (to_jsonb(OLD) -> 'category_classification_needed')
       AND (to_jsonb(NEW) - 'category_classification_needed')
           IS NOT DISTINCT FROM (to_jsonb(OLD) - 'category_classification_needed') THEN
        NEW.saved_at := OLD.saved_at;
    ELSIF to_jsonb(NEW) - review_keys = to_jsonb(OLD) - review_keys
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
