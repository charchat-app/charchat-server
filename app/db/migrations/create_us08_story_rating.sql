-- CREATE-US-08 스토리 등급 컬럼. 저장소 안의 초안이며 원격·운영 DB에 적용하지 않았다.
-- API 배포 전 기존 DB에 독립적으로 적용한다. 재실행해도 같은 결과이며, 이미 있는 컬럼·제약의
-- 정의가 다르면 바꾸지 않고 실패한다. 새 DB는 app/db/schema.sql로 같은 정의를 만든다.
-- 롤백(필요 시, 등급 데이터가 사라짐): 아래 제약 3개와 컬럼 5개를 DROP한다.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

LOCK TABLE public.stories IN ACCESS EXCLUSIVE MODE;

-- 기존 행은 기본값(SAFE·SAFE·not_reviewed, 사유·요청 시각 NULL)으로 채워진다.
ALTER TABLE public.stories
    ADD COLUMN IF NOT EXISTS requested_rating text NOT NULL DEFAULT 'SAFE',
    ADD COLUMN IF NOT EXISTS effective_rating text NOT NULL DEFAULT 'SAFE',
    ADD COLUMN IF NOT EXISTS rating_review_status text NOT NULL DEFAULT 'not_reviewed',
    ADD COLUMN IF NOT EXISTS rating_review_reason text,
    ADD COLUMN IF NOT EXISTS rating_requested_at timestamptz;

DO $us08$
DECLARE
    tbl CONSTANT regclass := 'public.stories'::regclass;
    col record;
    con record;
BEGIN
    FOR col IN
        SELECT * FROM (VALUES
            ('requested_rating', 'text'::regtype, true, '''SAFE''::text'),
            ('effective_rating', 'text'::regtype, true, '''SAFE''::text'),
            ('rating_review_status', 'text'::regtype, true, '''not_reviewed''::text'),
            ('rating_review_reason', 'text'::regtype, false, NULL),
            ('rating_requested_at', 'timestamptz'::regtype, false, NULL)
        ) AS v(name, typ, notnull, def)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_attribute a
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = tbl AND a.attname = col.name AND NOT a.attisdropped
              AND a.atttypid = col.typ AND a.attnotnull = col.notnull
              AND a.attidentity = '' AND a.attgenerated = ''
              AND pg_get_expr(d.adbin, d.adrelid) IS NOT DISTINCT FROM col.def
        ) THEN
            RAISE EXCEPTION 'CREATE-US-08: unexpected stories.% column definition', col.name;
        END IF;
    END LOOP;

    FOR con IN
        SELECT * FROM (VALUES
            ('stories_requested_rating_check',
             'CHECK ((requested_rating = ANY (ARRAY[''SAFE''::text, ''UNSAFE''::text])))',
             'CHECK (requested_rating IN (''SAFE'', ''UNSAFE''))'),
            ('stories_effective_rating_check',
             'CHECK ((effective_rating = ANY (ARRAY[''SAFE''::text, ''UNSAFE''::text])))',
             'CHECK (effective_rating IN (''SAFE'', ''UNSAFE''))'),
            ('stories_rating_review_status_check',
             'CHECK ((rating_review_status = ANY (ARRAY[''not_reviewed''::text, '
             || '''reviewing''::text, ''completed''::text, ''failed''::text])))',
             'CHECK (rating_review_status IN '
             || '(''not_reviewed'', ''reviewing'', ''completed'', ''failed''))')
        ) AS v(name, expected, ddl)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = tbl AND conname = con.name)
        THEN
            EXECUTE format('ALTER TABLE public.stories ADD CONSTRAINT %I %s', con.name, con.ddl);
        ELSIF NOT EXISTS (
            SELECT 1 FROM pg_constraint c WHERE c.conrelid = tbl AND c.conname = con.name
              AND c.contype = 'c' AND c.convalidated AND NOT c.connoinherit
              AND pg_get_constraintdef(c.oid) = con.expected
        ) THEN
            RAISE EXCEPTION 'CREATE-US-08: unexpected % definition', con.name;
        END IF;
    END LOOP;
END;
$us08$;

COMMIT;
