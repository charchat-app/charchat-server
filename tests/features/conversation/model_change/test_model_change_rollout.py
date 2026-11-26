"""CHAT-US-13 배포 SQL(kan23_model_change.sql): KAN-17까지 적용된 DB에 reported_model을 더해
schema.sql과 같은 정의가 되고, 다시 실행해도 같으며, 기존 생성 행은 NULL로 보존된다."""

from pathlib import Path

import psycopg
import pytest
from model_change_helpers import PLAYER, SESSIONS, change_model, model_id, send, turns
from psycopg.pq import TransactionStatus

ROOT = Path(__file__).resolve().parents[4]
MIGRATION = ROOT / "app/db/migrations/kan23_model_change.sql"


def generations_snapshot(db):
    """conversation_generations의 컬럼·제약·인덱스 정의."""
    columns = db.execute(
        """SELECT column_name, data_type, is_nullable, column_default, is_identity
           FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'conversation_generations'
           ORDER BY column_name"""
    ).fetchall()
    constraints = db.execute(
        """SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint
           WHERE conrelid = 'public.conversation_generations'::regclass ORDER BY 1"""
    ).fetchall()
    indexes = db.execute(
        """SELECT indexname, indexdef FROM pg_indexes
           WHERE schemaname = 'public' AND tablename = 'conversation_generations' ORDER BY 1"""
    ).fetchall()
    return columns, constraints, indexes


def drop_kan23_column(db):
    """KAN-23 컬럼을 지워 KAN-17까지 적용된 상태로 되돌린다."""
    db.execute("ALTER TABLE conversation_generations DROP COLUMN IF EXISTS reported_model")


@pytest.fixture
def migration(db):
    """마이그레이션 SQL. 테스트가 중간에 실패해도 다음 테스트가 쓸 schema.sql 정의로 되돌린다."""
    sql = MIGRATION.read_text(encoding="utf-8")
    yield sql
    if db.info.transaction_status != TransactionStatus.IDLE:
        db.execute("ROLLBACK")
    column = db.execute(
        """SELECT data_type FROM information_schema.columns
           WHERE table_name = 'conversation_generations' AND column_name = 'reported_model'"""
    ).fetchone()
    if column is None or column["data_type"] != "text":
        drop_kan23_column(db)
        db.execute(sql)


def test_chat_us_13_ac01_migration_on_kan17_db_matches_schema_and_keeps_rows(
    db, client, session, migration
):
    expected = generations_snapshot(db)
    assert send(client, session, "배포 전 메시지").status_code == 202
    assert (
        send(client, session, "#fail-before 배포 전 실패", request_id="req-00000002").status_code
        == 202
    )
    drop_kan23_column(db)
    before = db.execute("SELECT * FROM conversation_generations ORDER BY id").fetchall()
    assert [row["status"] for row in before] == ["completed", "failed"]
    assert "reported_model" not in before[0]

    db.execute(migration)
    db.execute(migration)  # 재실행해도 같은 결과

    assert generations_snapshot(db) == expected
    after = db.execute("SELECT * FROM conversation_generations ORDER BY id").fetchall()
    assert after == [{**row, "reported_model": None} for row in before]

    # 배포 뒤 기존 방에서 모델 변경·전송이 된다.
    terra = model_id(db, "테라")
    assert change_model(client, session, terra).status_code == 200
    sent = send(client, session, "배포 뒤 메시지", request_id="req-00000003")
    assert sent.status_code == 202
    row = db.execute(
        "SELECT model_id, reported_model FROM conversation_generations WHERE id = %s",
        (sent.json()["id"],),
    ).fetchone()
    assert row == {"model_id": terra, "reported_model": "gpt-6.1-terra"}
    room = turns(client, session)
    assert (room["model"], room["model_id"]) == ("테라", terra)
    assert client.get(f"{SESSIONS}/{session}", headers=PLAYER).json()["model"] == "테라"


def test_chat_us_13_migration_requires_kan17_generations_table(db, migration):
    db.execute("ALTER TABLE conversation_generations RENAME TO conversation_generations_kan23")
    try:
        with pytest.raises(
            psycopg.errors.RaiseException, match="conversation_generations not found"
        ):
            db.execute(migration)
        db.execute("ROLLBACK")
    finally:
        db.execute("ALTER TABLE conversation_generations_kan23 RENAME TO conversation_generations")


def test_chat_us_13_migration_aborts_on_unexpected_column_definition(db, migration):
    drop_kan23_column(db)
    db.execute("ALTER TABLE conversation_generations ADD COLUMN reported_model integer")
    with pytest.raises(psycopg.errors.RaiseException, match="unexpected .*reported_model"):
        db.execute(migration)
    db.execute("ROLLBACK")
    assert (
        db.execute(
            """SELECT data_type FROM information_schema.columns
               WHERE table_name = 'conversation_generations'
                 AND column_name = 'reported_model'"""
        ).fetchone()["data_type"]
        == "integer"
    )
