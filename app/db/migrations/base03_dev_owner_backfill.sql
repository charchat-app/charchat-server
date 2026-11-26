-- BASE-03 (KAN-2) 개발 DB 전용: 기존 개발용 owner_id를 계정과 dev 로그인 연결로 옮긴다.
-- 운영·공용 DB에는 실행하지 않는다. 절차: docs/base03-accounts-rollout.md「개발 DB 이관」
--
-- 6개 표(stories, images, conversation_sessions, user_profiles, user_play_settings,
-- creation_write_requests)의 owner_id마다 같은 id의 users(active)와
-- user_identities(provider='dev', provider_user_id=owner_id 문자열)를 만든다. 이미 있으면 두고 넘어간다.
-- 그러면 dev 로그인 키 "1"로 예전 X-User-Id: 1의 데이터가 그대로 보인다.
-- 동의 기록은 만들지 않는다(개발 데이터 호환용 active 처리).
--
-- 허용 DB: 이름이 _dev 또는 _test로 끝나는 DB, 또는 운영자가 세션 설정
-- charchat.dev_backfill_database에 현재 DB 이름을 직접 적은 경우(예: 로컬 Docker 기본 DB charchat).
--   PGOPTIONS='-c charchat.dev_backfill_database=charchat' psql ... --file=<이 파일>
-- 그 밖의 DB에서는 아무것도 바꾸지 않고 예외로 중단한다. 다시 실행해도 결과가 같다.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

DO $base03_backfill$
BEGIN
    IF NOT (
        current_database() ~ '_(dev|test)$'
        OR current_database()
            = COALESCE(current_setting('charchat.dev_backfill_database', true), '')
    ) THEN
        RAISE EXCEPTION 'BASE-03 backfill: database % is not an allow-listed dev database',
            current_database()
            USING HINT = 'Dev DBs only. Never run this on a shared or production database.';
    END IF;
    IF to_regclass('public.users') IS NULL OR to_regclass('public.user_identities') IS NULL THEN
        RAISE EXCEPTION 'BASE-03 backfill: apply base03_accounts.sql first';
    END IF;
END;
$base03_backfill$;

SELECT pg_advisory_xact_lock(hashtext('charchat.base03_accounts'));

CREATE TEMPORARY TABLE base03_owner_ids ON COMMIT DROP AS
SELECT DISTINCT owner_id FROM (
    SELECT owner_id FROM public.stories
    UNION SELECT owner_id FROM public.images
    UNION SELECT owner_id FROM public.conversation_sessions
    UNION SELECT owner_id FROM public.user_profiles
    UNION SELECT owner_id FROM public.user_play_settings
    UNION SELECT owner_id FROM public.creation_write_requests
) AS owners
WHERE owner_id > 0;

INSERT INTO public.users (id, status, onboarded_at)
SELECT owner_id, 'active', clock_timestamp() FROM base03_owner_ids
ORDER BY 1
ON CONFLICT (id) DO NOTHING;

-- 이메일·비밀번호 계정에는 dev 로그인 키를 붙이지 않는다(키 "1000"으로 남의 계정에 들어가는 것을 막음).
INSERT INTO public.user_identities (user_id, provider, provider_user_id)
SELECT o.owner_id, 'dev', o.owner_id::text FROM base03_owner_ids o
WHERE NOT EXISTS (
    SELECT 1 FROM public.user_password_credentials c WHERE c.user_id = o.owner_id
)
ORDER BY 1
ON CONFLICT (provider, provider_user_id) DO NOTHING;

-- 새 계정이 옮긴 id와 겹치지 않도록 identity 시퀀스를 최댓값 다음으로 맞춘다. 앞으로만 옮긴다:
-- base03_accounts.sql이 정한 값(1000 이상 등)보다 뒤로 가지 않는다.
DO $base03_backfill_seq$
DECLARE
    seq text := pg_get_serial_sequence('public.users', 'id');
    next_id bigint;
    wanted bigint;
BEGIN
    EXECUTE format('SELECT CASE WHEN is_called THEN last_value + 1 ELSE last_value END FROM %s', seq)
        INTO next_id;
    SELECT COALESCE(max(id), 0) + 1 INTO wanted FROM public.users;
    IF wanted > next_id THEN
        PERFORM setval(seq, wanted, false);
    END IF;
END;
$base03_backfill_seq$;

COMMIT;
