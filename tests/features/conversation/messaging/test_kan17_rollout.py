"""CHAT-US-02 배포 SQL: 팀 DDL이 없는 KAN-16 DB와 팀 DDL이 이미 적용된 공용 DB 모두에서
schema.sql과 같은 정의가 되고, 다시 실행해도 같으며, 정의가 다르면 전체를 되돌린다.

schema.sql에는 이후 배포분(CHAT-US-13 kan23_model_change.sql)도 들어 있으므로, 실제 적용 순서대로
KAN-17 → KAN-17 재실행 → KAN-23 → KAN-23 재실행한 결과를 schema.sql과 비교한다."""

from pathlib import Path

import psycopg
import pytest
from messaging_helpers import PLAYER, SESSIONS, send, session_characters, start_session, user_part
from psycopg.pq import TransactionStatus

ROOT = Path(__file__).resolve().parents[4]
MIGRATION = ROOT / "app/db/migrations/kan17_conversation_messaging.sql"
# KAN-17 뒤에 적용하는 배포분(CHAT-US-13). kan17 파일 자체는 바꾸지 않는다.
LATER_MIGRATIONS = (ROOT / "app/db/migrations/kan23_model_change.sql",)
TEAM_DDL = Path(__file__).with_name("team_ddl_20261120.sql")
KAN17_TABLES = (
    "models",
    "conversation_sessions",
    "conversation_messages",
    "conversation_message_parts",
    "conversation_generations",
)


def schema_snapshot(db):
    """KAN-17이 다루는 표의 컬럼·제약·인덱스 정의."""
    columns = db.execute(
        """SELECT table_name, column_name, data_type, is_nullable, column_default,
                  is_identity, identity_generation
           FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = ANY(%s)
           ORDER BY table_name, column_name""",
        (list(KAN17_TABLES),),
    ).fetchall()
    constraints = db.execute(
        """SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) AS def
           FROM pg_constraint
           WHERE conrelid = ANY(ARRAY(SELECT to_regclass(t) FROM unnest(%s::text[]) t))
           ORDER BY 1, 2""",
        (list(KAN17_TABLES),),
    ).fetchall()
    indexes = db.execute(
        """SELECT tablename, indexname, indexdef FROM pg_indexes
           WHERE schemaname = 'public' AND tablename = ANY(%s) ORDER BY 1, 2""",
        (list(KAN17_TABLES),),
    ).fetchall()
    return columns, constraints, indexes


def models(db):
    return [
        (row["name"], row["provider"], row["provider_model_id"])
        for row in db.execute(
            "SELECT name, provider, provider_model_id FROM models ORDER BY id"
        ).fetchall()
    ]


def drop_kan17_objects(db):
    """KAN-17 객체를 지워 origin/dev(KAN-16) 스키마 상태로 되돌린다."""
    with db.transaction():
        db.execute(
            """DROP TABLE IF EXISTS conversation_generations, conversation_message_parts,
                                    conversation_messages"""
        )
        db.execute("ALTER TABLE conversation_sessions DROP COLUMN IF EXISTS selected_model_id")
        db.execute("ALTER TABLE conversation_sessions DROP COLUMN IF EXISTS model")
        db.execute("DROP TABLE IF EXISTS models")


def apply_later_migrations(db):
    """KAN-17 뒤 배포분을 실제 순서대로 적용하고 다시 실행해도 같은지 함께 거친다."""
    for path in LATER_MIGRATIONS:
        later = path.read_text(encoding="utf-8")
        db.execute(later)
        db.execute(later)


@pytest.fixture
def migration(db):
    """마이그레이션 SQL. 테스트가 중간에 실패해도 다음 테스트가 쓸 스키마로 되돌린다."""
    sql = MIGRATION.read_text(encoding="utf-8")
    yield sql
    if db.info.transaction_status != TransactionStatus.IDLE:
        db.execute("ROLLBACK")
    if db.execute("SELECT to_regclass('conversation_generations') AS t").fetchone()["t"] is None:
        drop_kan17_objects(db)
        db.execute(sql)
    # KAN-17만 적용된 상태로 남지 않게 이후 배포분까지 맞춘다(멱등).
    apply_later_migrations(db)


def test_chat_us_02_migration_on_kan16_db_backfills_rooms_and_matches_schema(
    db, client, session, migration
):
    """(나) 팀 DDL이 없는 기존 DB: 기존 방은 기본 모델 루나로 채워진다."""
    expected = schema_snapshot(db)
    before = db.execute("SELECT * FROM conversation_sessions WHERE id = %s", (session,)).fetchone()
    drop_kan17_objects(db)

    db.execute(migration)
    db.execute(migration)  # 재실행해도 같은 결과
    apply_later_migrations(db)

    assert schema_snapshot(db) == expected
    assert models(db) == [("루나", "openai", "gpt-6-luna"), ("테라", "openai", "gpt-6.1-terra")]
    after = db.execute("SELECT * FROM conversation_sessions WHERE id = %s", (session,)).fetchone()
    luna = db.execute("SELECT id FROM models WHERE name = '루나'").fetchone()["id"]
    assert after == {**before, "selected_model_id": luna}

    room = client.get(f"{SESSIONS}/{session}", headers=PLAYER)
    assert room.status_code == 200
    assert room.json()["model"] == "루나"
    assert send(client, session, [user_part("배포 뒤 첫 메시지")]).status_code == 202
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert [m["role"] for m in turns["messages"]] == ["user", "assistant"]
    assert turns["model"] == "루나"


def test_chat_us_02_migration_on_remote_team_ddl_db_seeds_models_and_matches_schema(
    db, client, saved_story, migration
):
    """(가) 팀 DDL(2026-11-20)이 이미 적용된 공용 DB(models 비어 있음)."""
    expected = schema_snapshot(db)
    drop_kan17_objects(db)
    db.execute(TEAM_DDL.read_text(encoding="utf-8"))
    assert models(db) == []
    assert db.execute("SELECT to_regclass('conversation_generations') AS t").fetchone() == {
        "t": None
    }

    db.execute(migration)
    db.execute(migration)
    apply_later_migrations(db)

    assert schema_snapshot(db) == expected
    assert models(db) == [("루나", "openai", "gpt-6-luna"), ("테라", "openai", "gpt-6.1-terra")]

    story, intro, _ = saved_story
    session = start_session(client, story, intro)
    assert client.get(f"{SESSIONS}/{session}", headers=PLAYER).json()["model"] == "루나"
    chars = session_characters(client, session)
    parts = [
        user_part("유저로"),
        {"speaker_kind": "character", "character_id": chars["Bea"], "body": "Bea로"},
        {"speaker_kind": "narrator", "character_id": None, "body": "나레이터로"},
    ]
    assert send(client, session, parts).status_code == 202
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert turns["model"] == "루나"
    user_message, assistant = turns["messages"]
    assert [(p["speaker_kind"], p["speaker_name"]) for p in user_message["parts"]] == [
        ("user", "민서"),
        ("character", "Bea"),
        ("narrator", None),
    ]
    assert [(p["speaker_kind"], p["speaker_name"]) for p in assistant["parts"]] == [
        ("character", "도경"),
        ("narrator", None),
    ]


def test_chat_us_02_migration_keeps_existing_model_rows_by_name(db, migration):
    """모델 기준 행은 name으로 멱등: 이미 있는 이름은 다시 넣지 않는다."""
    drop_kan17_objects(db)
    db.execute(TEAM_DDL.read_text(encoding="utf-8"))
    db.execute(
        "INSERT INTO models (name, provider, provider_model_id) VALUES ('루나', 'openai', 'x')"
    )
    db.execute(migration)
    assert models(db) == [("루나", "openai", "x"), ("테라", "openai", "gpt-6.1-terra")]


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        (
            # 팀 정의와 다른 같은 이름의 표(예: 이전 초안)는 건너뛰지 않고 거절한다.
            """CREATE TABLE conversation_messages (
                   id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                   session_id bigint NOT NULL REFERENCES conversation_sessions(id),
                   seq integer NOT NULL
               )""",
            "unexpected public.conversation_messages definition",
        ),
        (
            "ALTER TABLE conversation_sessions ADD COLUMN model text",
            "conversation_sessions.model found",
        ),
    ],
)
def test_chat_us_02_migration_aborts_and_rolls_back_on_unexpected_definition(
    db, migration, prepare, message
):
    drop_kan17_objects(db)
    db.execute(prepare)
    with pytest.raises(psycopg.errors.RaiseException, match=message):
        db.execute(migration)
    db.execute("ROLLBACK")
    # 전체가 되돌아가 모델 표·세션 컬럼·생성 표가 하나도 생기지 않았다.
    assert db.execute(
        """SELECT to_regclass('models') AS models, to_regclass('conversation_generations') AS g,
                  EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_name = 'conversation_sessions'
                            AND column_name = 'selected_model_id') AS col"""
    ).fetchone() == {"models": None, "g": None, "col": False}
    with db.transaction():
        db.execute("DROP TABLE IF EXISTS conversation_messages")
        db.execute("ALTER TABLE conversation_sessions DROP COLUMN IF EXISTS model")
