"""CREATE-US-09-AC07: 확정 변경 집계 및 분류 결과 반영."""

from pathlib import Path
from threading import Event

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.features.creation.category_classification import worker
from app.features.creation.category_classification.worker import (
    ClassificationInput,
    CycleReport,
    RetryableClassificationError,
    run_cycle,
    run_hourly,
)
from app.main import create_app
from tests.features.creation.category_classification.test_first_jobs import (
    STORIES,
    Crash,
    Script,
    register,
)


def seed(db, *, state="private", pending=True, deleted=False):
    story_id = db.execute(
        """INSERT INTO stories(owner_id,save_state,world_description,category_classification_needed,
                              deleted_at)
           VALUES (100,%s,'기존 세계관',%s,CASE WHEN %s THEN clock_timestamp() END) RETURNING id""",
        (state, pending, deleted),
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO characters(story_id,name,description,position) "
        "VALUES (%s,'내부 이름','상세',0)",
        (story_id,),
    )
    db.execute("INSERT INTO story_categories VALUES (%s,'modern')", (story_id,))
    return story_id


def state(db, story):
    return db.execute(
        """SELECT category_classification_needed, saved_at,
                  ARRAY(SELECT category_code FROM story_categories WHERE story_id=s.id
                        ORDER BY category_code) AS categories
           FROM stories s WHERE id=%s""",
        (story,),
    ).fetchone()


def test_cycle_coalesces_and_filters_CREATE_US_09_AC07(db, db_url):
    story = seed(db)
    excluded = [seed(db, state="draft"), seed(db, pending=False), seed(db, deleted=True)]
    first = seed(db)
    db.execute("DELETE FROM story_categories WHERE story_id=%s", (first,))
    for world in ("첫 수정", "최종 수정"):
        db.execute("UPDATE stories SET world_description=%s WHERE id=%s", (world, story))
    saved_at = state(db, story)["saved_at"]
    calls = []

    def classify(value):
        calls.append((value.world_description, value.character_descriptions))
        return ("romance", "sf_fantasy")

    assert run_cycle(db_url, classify).completed == 1
    assert run_cycle(db_url, classify).selected == 0
    assert calls == [("최종 수정", ("상세",))]
    assert state(db, story) == dict(
        category_classification_needed=False,
        saved_at=saved_at,
        categories=["romance", "sf_fantasy"],
    )
    assert all(state(db, sid)["categories"] == ["modern"] for sid in excluded)
    assert state(db, first)["category_classification_needed"]


def test_cycle_preserves_previous_when_invalid_or_stale_CREATE_US_09_AC07(db, db_url):
    stale, invalid = seed(db), seed(db)
    db.execute("UPDATE stories SET world_description='유효하지 않음' WHERE id=%s", (invalid,))

    def classify(value):
        if value.world_description == "유효하지 않음":
            return ("romance", "unknown")
        with psycopg.connect(db_url, options="-c lock_timeout=2000") as editing:
            editing.execute("UPDATE stories SET world_description='새 버전' WHERE id=%s", (stale,))
        return ("romance",)

    report = run_cycle(db_url, classify)
    assert (report.selected, report.stale, report.failed) == (2, 1, 1)
    for sid in (stale, invalid):
        assert state(db, sid)["category_classification_needed"]
        assert state(db, sid)["categories"] == ["modern"]


def test_only_one_cycle_holds_the_worker_lock_CREATE_US_09_AC07(db, db_url):
    seed(db)

    def classify(_):
        def unexpected(_):
            raise AssertionError("overlapping cycle invoked classifier")

        assert run_cycle(db_url, unexpected).busy
        return ("romance",)

    assert run_cycle(db_url, classify).completed == 1


def test_hourly_scheduler_uses_one_hour_and_stops_CREATE_US_09_AC07(monkeypatch):
    calls = []
    waits = iter((False, False, True))
    now = 0
    monkeypatch.setattr(worker, "monotonic", lambda: now)

    class Stop(Event):
        def wait(self, timeout=None):
            nonlocal now
            assert timeout is not None
            calls.append(timeout)
            now += timeout
            return next(waits)

    def cycle():
        nonlocal now
        calls.append("cycle")
        now += 30
        return CycleReport()

    run_hourly(cycle, Stop())
    assert calls == [3600, "cycle", 3570, "cycle", 3570]


# --- KAN-15: 시간 단위 재분류의 상한 있는 재시도 (CREATE-US-09-AC02/AC07) ---

EDITED = "재분류 세계관"
EDITED_INPUT = ClassificationInput(EDITED, ("원래 상세",))
UNCHANGED = {"category_classification_needed": True, "categories": ["modern"]}


def edit_world(client, story_id, world, title="검수 스토리"):
    """기본 정보 API(서비스 경로)로 확정 저장한다."""
    payload = {"title": title, "subtitle": "한 줄 소개", "world_description": world}
    assert client.put(f"{STORIES}/{story_id}", json=payload).status_code == 200


def reclassifiable(db_url):
    """최초 분류가 끝나 유효 결과(modern)가 있고, 관련 확정 변경으로 재분류를 기다리는 스토리."""
    story_id = register(db_url, classifier=Script(["modern"]))
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"
        edit_world(client, story_id, EDITED)
    return story_id


def assert_not_called(db_url):
    """_infer가 분류기 예외를 삼키므로 unexpected 대신 호출 기록으로 확인한다."""
    never = Script(["romance"])
    run_cycle(db_url, never)
    assert never.calls == []


def categories_state(db, story_id):
    row = state(db, story_id)
    return {k: row[k] for k in ("category_classification_needed", "categories")}


@pytest.mark.usefixtures("dev_user_header_enabled")
def test_retryable_failures_stop_at_three_and_keep_links_CREATE_US_09_AC02_AC07(db, db_url):
    story_id = reclassifiable(db_url)
    classify = Script(RetryableClassificationError("503"))
    report = run_cycle(db_url, classify)
    # 같은 확정 입력을 바로 재시도해 최초 1회 + 재시도 2회, 모두 3회에서 멈춘다.
    assert classify.calls == [EDITED_INPUT] * 3
    assert report.failed == 1 and report.completed == 0
    assert categories_state(db, story_id) == UNCHANGED
    # 다음 주기에도 상한을 다시 채우지 않는다. 기존 연결과 분류 필요 표시는 유지한다.
    assert_not_called(db_url)
    assert categories_state(db, story_id) == UNCHANGED


@pytest.mark.usefixtures("dev_user_header_enabled")
@pytest.mark.parametrize("outcome", [ValueError("upstream rejected"), ["unknown"], []])
def test_permanent_or_invalid_runs_once_and_not_next_cycle_CREATE_US_09_AC02_AC07(
    db, db_url, outcome
):
    story_id = reclassifiable(db_url)
    classify = Script(outcome)
    assert run_cycle(db_url, classify).failed == 1
    assert classify.calls == [EDITED_INPUT]
    assert categories_state(db, story_id) == UNCHANGED
    assert_not_called(db_url)
    assert categories_state(db, story_id) == UNCHANGED


@pytest.mark.usefixtures("dev_user_header_enabled")
def test_transient_then_valid_replaces_links_CREATE_US_09_AC02_AC07(db, db_url):
    story_id = reclassifiable(db_url)
    classify = Script(RetryableClassificationError("429"), ["romance", "sf_fantasy"])
    assert run_cycle(db_url, classify).completed == 1
    assert classify.calls == [EDITED_INPUT] * 2
    assert categories_state(db, story_id) == {
        "category_classification_needed": False,
        "categories": ["romance", "sf_fantasy"],
    }


@pytest.mark.usefixtures("dev_user_header_enabled")
def test_only_relevant_applied_edit_reopens_budget_CREATE_US_09_AC02_AC07(db, db_url):
    story_id = reclassifiable(db_url)
    assert run_cycle(db_url, Script(["unknown"])).failed == 1  # 예산 소진
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"
        edit_world(client, story_id, EDITED, title="제목만 변경")  # 분류 입력과 무관
        assert_not_called(db_url)
        edit_world(client, story_id, EDITED, title="제목만 변경")  # 실제 변화 없음
        assert_not_called(db_url)
        assert categories_state(db, story_id) == UNCHANGED
        edit_world(client, story_id, "최종 세계관", title="제목만 변경")  # 관련 변경 반영
    classify = Script(["romance"])
    assert run_cycle(db_url, classify).completed == 1
    assert classify.calls == [ClassificationInput("최종 세계관", ("원래 상세",))]
    assert categories_state(db, story_id) == {
        "category_classification_needed": False,
        "categories": ["romance"],
    }


@pytest.mark.usefixtures("dev_user_header_enabled")
def test_edit_during_transient_inference_skips_superseded_retry_CREATE_US_09_AC02_AC07(db, db_url):
    story_id = reclassifiable(db_url)
    latest = ClassificationInput("편집 중 변경", ("원래 상세",))
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"

        def edit_once(_):
            # 추론 중 스토리 행 잠금이 없으므로 서비스 경로 저장이 바로 끝난다.
            if len(classify.calls) == 1:
                edit_world(client, story_id, latest.world_description)

        classify = Script(RetryableClassificationError("503"), ["romance"], effect=edit_once)
        run_cycle(db_url, classify)
    # 바뀌기 전 입력은 같은 주기에서 다시 보내지 않는다. 이후 호출은 최신 입력뿐이다.
    assert classify.calls[0] == EDITED_INPUT
    assert all(call == latest for call in classify.calls[1:])
    # 관련 변경으로 예산이 다시 열려 최신 입력이 반영된다(같은 주기든 다음 주기든).
    after = Script(["romance"])
    run_cycle(db_url, after)
    assert all(call == latest for call in after.calls)
    assert categories_state(db, story_id) == {
        "category_classification_needed": False,
        "categories": ["romance"],
    }


# --- 재분류 시도 기록(story_category_reclassification_attempts) 수준 확인 ---


def ledger(db, story_id):
    return db.execute(
        """SELECT attempts, failure_reason FROM story_category_reclassification_attempts
           WHERE story_id=%s""",
        (story_id,),
    ).fetchone()


@pytest.mark.usefixtures("dev_user_header_enabled")
def test_budget_is_not_refilled_across_stop_and_restart_CREATE_US_09_AC02_AC07(db, db_url):
    story_id = reclassifiable(db_url)
    stop = Event()

    def stop_after_call(_):
        stop.set()

    first = Script(RetryableClassificationError("503"), effect=stop_after_call)
    report = run_cycle(db_url, first, should_stop=stop.is_set)
    # 일시 실패를 기록한 뒤 즉시 재시도 전에 멈춘다. 쓴 시도 1회는 남는다.
    assert report.stopped and first.calls == [EDITED_INPUT]
    assert ledger(db, story_id) == {"attempts": 1, "failure_reason": "temporary"}
    # 재시작한 주기는 남은 2회만 쓴다(누적 3회). 그 뒤 주기는 호출하지 않는다.
    restarted = Script(RetryableClassificationError("503"))
    assert run_cycle(db_url, restarted).failed == 1
    assert restarted.calls == [EDITED_INPUT] * 2
    assert ledger(db, story_id) == {"attempts": 3, "failure_reason": "temporary"}
    assert_not_called(db_url)
    assert categories_state(db, story_id) == UNCHANGED


@pytest.mark.usefixtures("dev_user_header_enabled")
def test_stop_at_reservation_rolls_back_attempt_CREATE_US_09_AC02_AC07(db, db_url):
    story_id = reclassifiable(db_url)
    checks = []

    def stop_on_final_check():
        # 1) 스토리 시작 전 확인은 통과, 2) 시도 기록 직후 같은 트랜잭션 안 확인에서 멈춘다.
        checks.append(True)
        return len(checks) >= 2

    never = Script(["romance"])
    assert run_cycle(db_url, never, should_stop=stop_on_final_check).stopped
    assert never.calls == [] and len(checks) == 2
    assert ledger(db, story_id) is None
    # 롤백됐으므로 다음 주기는 예산 3회를 그대로 쓴다.
    classify = Script(["romance"])
    assert run_cycle(db_url, classify).completed == 1
    assert classify.calls == [EDITED_INPUT]
    assert ledger(db, story_id) is None  # 성공 반영 시 기록도 지운다.


@pytest.mark.usefixtures("dev_user_header_enabled")
def test_crashed_attempts_count_and_final_one_ends_without_call_CREATE_US_09_AC02_AC07(db, db_url):
    story_id = reclassifiable(db_url)
    for attempt in (1, 2, 3):
        with pytest.raises(Crash):
            run_cycle(db_url, Script(Crash()))
        # 공급자 호출 전에 센 시도는 비정상 종료해도 남는다(실행 중 = 사유 없음).
        assert ledger(db, story_id) == {"attempts": attempt, "failure_reason": None}
    never = Script(["romance"])
    assert run_cycle(db_url, never).failed == 1
    assert never.calls == []
    assert ledger(db, story_id) == {"attempts": 3, "failure_reason": "interrupted"}
    assert run_cycle(db_url, never).selected == 0
    assert categories_state(db, story_id) == UNCHANGED


@pytest.mark.usefixtures("dev_user_header_enabled")
def test_late_aba_edit_during_inference_is_not_saved_CREATE_US_09_AC02_AC07(db, db_url):
    story_id = reclassifiable(db_url)
    with TestClient(create_app(db_url)) as client:
        client.headers["X-User-Id"] = "100"

        def aba(_):
            # 입력이 B로 바뀌었다가 원래(A)로 돌아온다. 스냅샷은 같아도 예약 토큰은 무효다.
            edit_world(client, story_id, "중간 세계관")
            edit_world(client, story_id, EDITED)

        classify = Script(["romance"], effect=aba)
        report = run_cycle(db_url, classify)
    assert (report.completed, report.stale) == (0, 1)
    assert classify.calls == [EDITED_INPUT]
    # 옛 시도의 결과는 반영하지 않고, 새 확정 변경은 다음 주기에 새 예산으로 처리한다.
    assert categories_state(db, story_id) == UNCHANGED
    assert ledger(db, story_id) is None
    after = Script(["sf_fantasy"])
    assert run_cycle(db_url, after).completed == 1
    assert after.calls == [EDITED_INPUT]
    assert categories_state(db, story_id) == {
        "category_classification_needed": False,
        "categories": ["sf_fantasy"],
    }


def test_legacy_classified_story_without_jobs_stays_eligible_CREATE_US_09_AC07(db, db_url):
    # 작업·시도 기록 없이 결과와 분류 필요 표시만 있는 기존 스토리도 재분류 대상이다.
    story_id = seed(db)
    classify = Script(["romance"])
    assert run_cycle(db_url, classify).completed == 1
    assert len(classify.calls) == 1
    assert ledger(db, story_id) is None


RECLASSIFY_MIGRATION = Path(__file__).resolve().parents[4] / (
    "app/db/migrations/create_us09_reclassification_attempts.sql"
)
# app/db/schema.sql의 같은 표 정의. 빈칸을 바꿔 잘못된 기존 표를 만든다.
ATTEMPTS_DDL = """CREATE TABLE story_category_reclassification_attempts (
    story_id bigint {pk} REFERENCES stories(id),
    attempts integer NOT NULL CHECK (attempts BETWEEN 1 AND 3),
    attempt_token uuid NOT NULL {unique},
    failure_reason text CHECK (failure_reason IN ('permanent', 'temporary', 'interrupted')),
    updated_at timestamptz NOT NULL {default}
)"""
CANONICAL = {"pk": "PRIMARY KEY", "unique": "UNIQUE", "default": "DEFAULT clock_timestamp()"}


@pytest.fixture
def migration_db(db):
    # 세션 공유 스키마이므로, 테스트가 중간에 실패해도 지우거나 바꾼 표를 정의대로 되돌린다.
    # 되돌리기는 검사 대상인 마이그레이션에 의존하지 않는다.
    yield db
    db.execute("DROP TABLE IF EXISTS story_category_reclassification_attempts")
    db.execute(ATTEMPTS_DDL.format(**CANONICAL))


def test_reclassification_migration_replay_preserves_rows_CREATE_US_09_AC02_AC07(
    migration_db, db_url
):
    db = migration_db
    sql = RECLASSIFY_MIGRATION.read_text(encoding="utf-8")
    db.execute("DROP TABLE story_category_reclassification_attempts")
    db.execute(sql)
    story_id = seed(db)
    assert run_cycle(db_url, Script(["unknown"])).failed == 1
    before = db.execute(
        "SELECT * FROM story_category_reclassification_attempts WHERE story_id=%s", (story_id,)
    ).fetchone()
    assert before["attempts"] == 1 and before["failure_reason"] == "permanent"
    db.execute(sql)  # 재실행해도 표를 다시 만들거나 행을 지우지 않는다(기존 표 검사 통과).
    after = db.execute(
        "SELECT * FROM story_category_reclassification_attempts WHERE story_id=%s", (story_id,)
    ).fetchone()
    assert after == before


def test_reclassification_migration_accepts_schema_sql_table_CREATE_US_09_AC02_AC07(
    migration_db,
):
    # schema.sql로 만든 표(새 설치 환경)에 적용해도 검사를 통과하고 아무것도 바꾸지 않는다.
    migration_db.execute(RECLASSIFY_MIGRATION.read_text(encoding="utf-8"))
    assert attempts_table_exists(migration_db) is not None


def apply_expecting_failure(db_url, match):
    # 실패한 BEGIN 블록이 남지 않도록 별도 연결에서 실행하고 닫아 되돌린다.
    sql = RECLASSIFY_MIGRATION.read_text(encoding="utf-8")
    with psycopg.connect(db_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match=match):
            conn.execute(sql)


def attempts_table_exists(db):
    return db.execute(
        "SELECT to_regclass('public.story_category_reclassification_attempts') AS t"
    ).fetchone()["t"]


@pytest.mark.parametrize(
    ("break_sql", "restore_sql"),
    [
        (
            "ALTER TABLE stories ALTER COLUMN category_classification_needed DROP NOT NULL",
            "ALTER TABLE stories ALTER COLUMN category_classification_needed SET NOT NULL",
        ),
        (
            "ALTER TABLE story_categories RENAME TO us09_story_categories_off",
            "ALTER TABLE us09_story_categories_off RENAME TO story_categories",
        ),
    ],
    ids=["flag-not-null", "story-categories"],
)
def test_reclassification_migration_requires_category_baseline_CREATE_US_09_AC02_AC07(
    migration_db, db_url, break_sql, restore_sql
):
    db = migration_db
    db.execute("DROP TABLE story_category_reclassification_attempts")
    db.execute(break_sql)
    try:
        apply_expecting_failure(db_url, "category baseline missing")
        assert attempts_table_exists(db) is None  # 기준선이 없으면 아무것도 만들지 않는다.
    finally:
        db.execute(restore_sql)
    db.execute(RECLASSIFY_MIGRATION.read_text(encoding="utf-8"))
    assert attempts_table_exists(db) is not None


def table_definition(db):
    # 제약 정의(이름 제외)와 deferrable 여부, updated_at 기본값, 유일 인덱스 정의.
    return db.execute(
        """SELECT
             (SELECT array_agg(concat_ws(' ', contype, pg_get_constraintdef(oid), condeferrable)
                               ORDER BY contype, pg_get_constraintdef(oid))
                FROM pg_constraint
               WHERE conrelid = 'story_category_reclassification_attempts'::regclass) AS cons,
             (SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d
                JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
               WHERE d.adrelid = 'story_category_reclassification_attempts'::regclass
                 AND a.attname = 'updated_at') AS updated_at_default,
             (SELECT array_agg(pg_get_indexdef(indexrelid)
                               ORDER BY pg_get_indexdef(indexrelid))
                FROM pg_index
               WHERE indrelid = 'story_category_reclassification_attempts'::regclass) AS idx"""
    ).fetchone()


@pytest.mark.parametrize(
    ("overrides", "extra_sql", "match"),
    [
        ({"pk": "NOT NULL"}, None, r"needs PRIMARY KEY \(story_id\)"),
        (
            {"pk": "PRIMARY KEY DEFERRABLE INITIALLY IMMEDIATE"},
            None,
            r"needs PRIMARY KEY \(story_id\)",
        ),
        (
            {"pk": "PRIMARY KEY DEFERRABLE INITIALLY DEFERRED"},
            None,
            r"needs PRIMARY KEY \(story_id\)",
        ),
        # KAN-15: 정상 PK 옆 DEFERRABLE UNIQUE (story_id)도
        # ON CONFLICT (story_id) 중재자로 추론돼 새 키 upsert가 55000으로 실패한다.
        (
            {},
            "ALTER TABLE story_category_reclassification_attempts ADD UNIQUE (story_id) DEFERRABLE",
            r"has a deferrable unique index on story_id",
        ),
        ({"unique": ""}, None, r"needs UNIQUE \(attempt_token\)"),
        (
            {"unique": ""},
            "CREATE UNIQUE INDEX us09_token_idx"
            " ON story_category_reclassification_attempts (attempt_token)",
            r"needs UNIQUE \(attempt_token\)",
        ),
        ({"default": ""}, None, r"needs updated_at DEFAULT clock_timestamp\(\)"),
        ({"default": "DEFAULT now()"}, None, r"needs updated_at DEFAULT clock_timestamp\(\)"),
        # 정의에 없는 추가 유일 규칙(예: UNIQUE (attempts))은 다른 스토리의 새 시도
        # (attempts=1) 삽입을 막으므로 받아들이지 않는다.
        (
            {},
            "ALTER TABLE story_category_reclassification_attempts ADD UNIQUE (attempts)",
            r"unexpected uniqueness rules",
        ),
    ],
    ids=[
        "no-pk",
        "deferrable-pk",
        "deferred-pk",
        "pk-with-deferrable-story-unique",
        "no-unique",
        "unique-index-only",
        "no-default",
        "wrong-default",
        "extra-unique-attempts",
    ],
)
def test_reclassification_migration_rejects_malformed_table_CREATE_US_09_AC02_AC07(
    migration_db, db_url, overrides, extra_sql, match
):
    # 열 형태는 같지만 PK(즉시 검사, ON CONFLICT 중재자)·attempt_token 유일 제약·
    # updated_at 기본값이 정의와 다른 기존 표는 받아들이지 않는다.
    db = migration_db
    db.execute("DROP TABLE story_category_reclassification_attempts")
    db.execute(ATTEMPTS_DDL.format(**{**CANONICAL, **overrides}))
    if extra_sql:
        db.execute(extra_sql)
    before = table_definition(db)
    apply_expecting_failure(db_url, match)
    assert table_definition(db) == before  # 기존 표를 고치지 않고 전체를 되돌린다.
