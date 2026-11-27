-- CREATE-US-09-AC02/AC07 시간 재분류의 확정 변경당 시도 기록(운영 기록) 테이블.
-- 이 파일은 새 테이블만 만든다. 기존 표·데이터는 바꾸지 않으며 백필하지 않는다.
-- (행이 없는 스토리는 이번 확정 변경의 시도가 아직 없는 것으로 보고 예산 3회를 새로 쓴다.)
-- create_us09_first_classification_jobs.sql 뒤, 서버·분류 실행기 배포 전에 적용한다.
-- 전제(기준선): stories.category_classification_needed boolean NOT NULL, categories,
-- story_categories가 이미 있어야 한다. 없으면 아무것도 만들지 않고 예외로 중단한다(README 참고).
-- 정의는 app/db/schema.sql의 같은 표와 같다. 여러 번 실행해도 기존 행을 보존한다.
-- 이미 같은 이름의 표가 있으면 만들지 않고, 정의가 다르면 예외로 전체를 되돌린다.
-- 롤백: 새 서버 코드를 되돌린 뒤 DROP TABLE public.story_category_reclassification_attempts;
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

DO $us09$
BEGIN
    IF to_regclass('public.stories') IS NULL THEN
        RAISE EXCEPTION 'CREATE-US-09: public.stories not found';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute a
         WHERE a.attrelid = 'public.stories'::regclass
           AND a.attname = 'category_classification_needed' AND NOT a.attisdropped
           AND a.atttypid = 'boolean'::regtype AND a.attnotnull
    ) THEN
        RAISE EXCEPTION 'CREATE-US-09: category baseline missing'
            USING DETAIL = 'stories.category_classification_needed boolean NOT NULL not found';
    END IF;
    IF to_regclass('public.categories') IS NULL OR to_regclass('public.story_categories') IS NULL THEN
        RAISE EXCEPTION 'CREATE-US-09: category baseline missing'
            USING DETAIL = 'public.categories or public.story_categories not found';
    END IF;
END;
$us09$;

CREATE TABLE IF NOT EXISTS public.story_category_reclassification_attempts (
    story_id bigint PRIMARY KEY REFERENCES public.stories(id),
    attempts integer NOT NULL CHECK (attempts BETWEEN 1 AND 3),
    attempt_token uuid NOT NULL UNIQUE,
    failure_reason text CHECK (failure_reason IN ('permanent', 'temporary', 'interrupted')),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

DO $us09$
DECLARE
    rel constant regclass := 'public.story_category_reclassification_attempts'::regclass;
    actual text;
    pk_index oid;
    token_index oid;
    story_fk oid;
    expected_checks text[];
    actual_checks text[];
BEGIN
    SELECT string_agg(
               format('%s %s %s', a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull::text),
               ', ' ORDER BY a.attnum)
      INTO actual
      FROM pg_attribute a
     WHERE a.attrelid = 'public.story_category_reclassification_attempts'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
    IF actual IS DISTINCT FROM
       'story_id bigint true, attempts integer true, attempt_token uuid true, '
       'failure_reason text false, updated_at timestamp with time zone true' THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts has an unexpected shape'
            USING DETAIL = actual;
    END IF;

    -- 제약은 이름이 아니라 대상 열·참조 대상·식으로 확인한다.
    -- PK는 ON CONFLICT (story_id)의 중재자이므로 즉시 검사(DEFERRABLE 아님)·유효해야 한다.
    SELECT c.conindid INTO pk_index
      FROM pg_constraint c
      JOIN pg_index i ON i.indexrelid = c.conindid
     WHERE c.conrelid = rel AND c.contype = 'p'
       AND NOT c.condeferrable AND c.convalidated
       AND i.indisvalid AND i.indimmediate
       AND c.conkey = ARRAY[(SELECT attnum FROM pg_attribute
                              WHERE attrelid = rel AND attname = 'story_id')];
    IF pk_index IS NULL THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts needs PRIMARY KEY (story_id)'
            USING DETAIL = 'non-deferrable, validated primary key on story_id required';
    END IF;
    -- 정상 PK가 있어도 ON CONFLICT (story_id)는 story_id 하나만 키로 갖는 유효한 유일 인덱스를
    -- 모두 중재자로 추론한다. 그중 지연 검사 인덱스가 있으면 upsert가 55000으로 실패하므로 거부한다.
    -- INCLUDE 열이 있어도 키 열이 story_id 하나면 추론 대상이다. 식·부분 인덱스는 WHERE 없는
    -- ON CONFLICT (story_id)로 추론되지 않으므로 대상에서 뺀다.
    IF EXISTS (
        SELECT 1 FROM pg_index i
         WHERE i.indrelid = rel AND i.indisunique AND i.indisvalid AND NOT i.indimmediate
           AND i.indnkeyatts = 1
           AND i.indkey[0] = (SELECT attnum FROM pg_attribute
                               WHERE attrelid = rel AND attname = 'story_id')
           AND i.indexprs IS NULL AND i.indpred IS NULL
    ) THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts has a deferrable unique index on story_id'
            USING DETAIL = 'ON CONFLICT (story_id) would infer it as an arbiter; only the immediate primary key may cover story_id';
    END IF;
    SELECT c.oid INTO story_fk
      FROM pg_constraint c
     WHERE c.conrelid = rel AND c.contype = 'f'
       AND NOT c.condeferrable AND c.convalidated
       AND c.confrelid = 'public.stories'::regclass
       AND c.conkey = ARRAY[(SELECT attnum FROM pg_attribute
                              WHERE attrelid = rel AND attname = 'story_id')]
       AND c.confkey = ARRAY[(SELECT attnum FROM pg_attribute
                               WHERE attrelid = 'public.stories'::regclass AND attname = 'id')]
     ORDER BY c.oid
     LIMIT 1;
    IF story_fk IS NULL THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts needs story_id REFERENCES stories(id)'
            USING DETAIL = 'non-deferrable, validated foreign key on story_id required';
    END IF;
    -- 정의에 없는 FK(다른 열·다른 참조 대상, 중복 FK 포함)는 새 시도 기록 삽입을 막을 수 있으므로 거부한다.
    SELECT string_agg(pg_get_constraintdef(c.oid), '; ' ORDER BY pg_get_constraintdef(c.oid))
      INTO actual
      FROM pg_constraint c
     WHERE c.conrelid = rel AND c.contype = 'f' AND c.oid <> story_fk;
    IF actual IS NOT NULL THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts has unexpected foreign keys'
            USING DETAIL = actual;
    END IF;
    -- int2vector(indkey)는 배열 하한이 0이라 ARRAY[...]와 같다고 나오지 않는다. conkey로 비교한다.
    SELECT c.conindid INTO token_index
      FROM pg_constraint c
      JOIN pg_index i ON i.indexrelid = c.conindid
     WHERE c.conrelid = rel AND c.contype = 'u'
       AND NOT c.condeferrable AND c.convalidated
       AND i.indisvalid AND i.indimmediate
       AND c.conkey = ARRAY[(SELECT attnum FROM pg_attribute
                              WHERE attrelid = rel AND attname = 'attempt_token')]
     ORDER BY c.oid
     LIMIT 1;
    IF token_index IS NULL THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts needs UNIQUE (attempt_token)'
            USING DETAIL = 'non-deferrable, validated unique constraint on attempt_token required';
    END IF;
    -- 확인한 PK·attempt_token 유일 제약 외의 유일·배제 인덱스(제약 여부·유효성·부분·식 무관)는
    -- 정의에 없는 유일 규칙이다(예: UNIQUE (attempts)는 다른 스토리의 첫 시도 삽입을 막는다).
    -- 일반(비유일) 인덱스는 삽입을 막지 않으므로 대상에서 뺀다.
    SELECT string_agg(coalesce(pg_get_constraintdef(c.oid), pg_get_indexdef(i.indexrelid)), '; '
                      ORDER BY coalesce(pg_get_constraintdef(c.oid), pg_get_indexdef(i.indexrelid)))
      INTO actual
      FROM pg_index i
      LEFT JOIN pg_constraint c
        ON c.conindid = i.indexrelid AND c.conrelid = rel AND c.contype IN ('p', 'u', 'x')
     WHERE i.indrelid = rel AND (i.indisunique OR i.indisexclusion)
       AND i.indexrelid NOT IN (pk_index, token_index);
    IF actual IS NOT NULL THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts has unexpected uniqueness rules'
            USING DETAIL = actual;
    END IF;

    -- CHECK 식과 updated_at 기본값은 같은 정의로 만든 임시 표의 정규화 결과와 비교한다
    -- (버전별 표기 차이 회피). 예약 INSERT는 updated_at을 넣지 않고 기본값에 맡긴다.
    CREATE TEMP TABLE us09_reclassification_check_ref (
        attempts integer CHECK (attempts BETWEEN 1 AND 3),
        failure_reason text CHECK (failure_reason IN ('permanent', 'temporary', 'interrupted')),
        updated_at timestamptz DEFAULT clock_timestamp()
    ) ON COMMIT DROP;
    IF (SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d
          JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
         WHERE d.adrelid = rel AND a.attname = 'updated_at')
       IS DISTINCT FROM
       (SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d
          JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
         WHERE d.adrelid = 'us09_reclassification_check_ref'::regclass
           AND a.attname = 'updated_at') THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts needs updated_at DEFAULT clock_timestamp()';
    END IF;
    SELECT array_agg(d ORDER BY d) INTO expected_checks
      FROM (SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c
             WHERE c.conrelid = 'us09_reclassification_check_ref'::regclass
               AND c.contype = 'c') ref;
    SELECT array_agg(d ORDER BY d) INTO actual_checks
      FROM (SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c
             WHERE c.conrelid = rel AND c.contype = 'c') cur;
    DROP TABLE us09_reclassification_check_ref;
    IF actual_checks IS DISTINCT FROM expected_checks THEN
        RAISE EXCEPTION 'CREATE-US-09: story_category_reclassification_attempts has unexpected CHECK constraints'
            USING DETAIL = coalesce(array_to_string(actual_checks, '; '), '(none)');
    END IF;
END;
$us09$;

COMMIT;
