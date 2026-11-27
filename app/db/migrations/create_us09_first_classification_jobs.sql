-- CREATE-US-09-AC02 최초 카테고리 분류 작업 기록(transactional outbox) 테이블.
-- 이 파일은 새 테이블과 그 인덱스만 만든다. 기존 표·데이터는 바꾸지 않으며 백필하지 않는다.
-- (적용 전에 이미 등록되어 최초 결과가 없는 스토리는 작업 기록이 없어 복구 대상이 아니다.)
-- 서버 배포 전에 적용한다. 정의는 app/db/schema.sql의 같은 표와 같다.
-- 이미 같은 이름의 표가 있으면 만들지 않고, 정의가 다르면 예외로 전체를 되돌린다.
-- 롤백: 새 서버 코드를 되돌린 뒤 DROP TABLE public.story_first_classification_jobs;
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

DO $us09$
BEGIN
    IF to_regclass('public.stories') IS NULL THEN
        RAISE EXCEPTION 'CREATE-US-09: public.stories not found';
    END IF;
END;
$us09$;

CREATE TABLE IF NOT EXISTS public.story_first_classification_jobs (
    story_id bigint PRIMARY KEY REFERENCES public.stories(id),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    failure_reason text
        CHECK (failure_reason IN ('temporary', 'permanent', 'interrupted', 'stale', 'ineligible')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS story_first_classification_jobs_queued_idx
    ON public.story_first_classification_jobs (story_id) WHERE status IN ('pending', 'running');

DO $us09$
DECLARE
    actual text;
BEGIN
    SELECT string_agg(
               format('%s %s %s', a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull::text),
               ', ' ORDER BY a.attnum)
      INTO actual
      FROM pg_attribute a
     WHERE a.attrelid = 'public.story_first_classification_jobs'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
    IF actual IS DISTINCT FROM
       'story_id bigint true, status text true, attempts integer true, '
       'failure_reason text false, created_at timestamp with time zone true, '
       'updated_at timestamp with time zone true' THEN
        RAISE EXCEPTION 'CREATE-US-09: story_first_classification_jobs has an unexpected shape'
            USING DETAIL = actual;
    END IF;
END;
$us09$;

COMMIT;
