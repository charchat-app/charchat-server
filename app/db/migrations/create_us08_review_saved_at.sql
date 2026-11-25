-- CREATE-US-08 검토 메타데이터만 바뀐 UPDATE에서 stories.saved_at 유지. 저장소 안의 초안이며 원격·운영
-- DB에 적용하지 않았다. API 배포 전 기존 DB에 독립적으로 적용한다(등급 컬럼 마이그레이션
-- create_us08_story_rating.sql과 순서 무관: 컬럼 이름을 jsonb 키로만 비교한다). 재실행해도 같은
-- 결과다. 함수 본문은 app/db/schema.sql의 enforce_story_lifecycle()과 같아야 한다.
-- 삭제·복구·소유자 변경·상태 전환 금지는 그대로 두고 saved_at 갱신 조건만 바꾼다.
-- stories_lifecycle_guard 트리거가 기대한 정의(BEFORE UPDATE OR DELETE, 행 단위, 활성)가 아니면
-- 바꾸지 않고 실패한다(한 트랜잭션이라 함수 교체도 되돌아간다).
-- 롤백(필요 시): 이 수정 전 함수 본문(마지막 saved_at 줄이 무조건 clock_timestamp())으로 되돌린다.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

-- 교체 중 다른 쓰기가 앞뒤 정의를 섞어 보지 않게 쓰기만 막는다(읽기는 허용).
LOCK TABLE public.stories IN SHARE ROW EXCLUSIVE MODE;

CREATE OR REPLACE FUNCTION public.enforce_story_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    review_keys CONSTANT text[] := ARRAY[
        'effective_rating', 'rating_review_status', 'rating_review_reason',
        'rating_requested_at', 'saved_at'];
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
