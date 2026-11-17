-- CREATE-US-02 요청 중복방지. 운영/공유 DB에 자동 실행하지 않는다.
CREATE TABLE expression_write_requests (
    owner_id bigint NOT NULL,
    request_id text NOT NULL,
    story_id bigint NOT NULL REFERENCES stories(id),
    request_hash text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (owner_id, request_id)
);
-- 응답 기록과 표현 설정을 같은 트랜잭션에 커밋한다.
-- 오래된 요청 재전송으로 최신 표현을 덮어쓰지 않는다. 보존/정리 정책은 별도 결정한다.
