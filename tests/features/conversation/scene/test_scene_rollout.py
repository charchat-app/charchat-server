"""CHAT-US-08 배포 SQL(kan22_scene_changes.sql): 장면 변경 기록 표를 더한다.

schema.sql과 같은 정의가 되고, 다시 실행해도 같으며, 같은 이름의 다른 표가 있으면 전체를 되돌린다.
"""

from pathlib import Path

import psycopg
import pytest
from psycopg.pq import TransactionStatus

ROOT = Path(__file__).resolve().parents[4]
MIGRATION = ROOT / "app/db/migrations/kan22_scene_changes.sql"


def snapshot(db):
    columns = db.execute(
        """SELECT column_name, data_type, is_nullable, column_default, is_identity
           FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'conversation_scene_changes'
           ORDER BY column_name"""
    ).fetchall()
    constraints = db.execute(
        """SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint
           WHERE conrelid = to_regclass('public.conversation_scene_changes') ORDER BY 1"""
    ).fetchall()
    indexes = db.execute(
        """SELECT indexname, indexdef FROM pg_indexes
           WHERE schemaname = 'public' AND tablename = 'conversation_scene_changes' ORDER BY 1"""
    ).fetchall()
    return columns, constraints, indexes


@pytest.fixture
def migration(db):
    sql = MIGRATION.read_text(encoding="utf-8")
    yield sql
    if db.info.transaction_status != TransactionStatus.IDLE:
        db.execute("ROLLBACK")
    db.execute("DROP TABLE IF EXISTS conversation_scene_changes")
    db.execute(sql)


def test_chat_us_08_migration_creates_table_like_schema_and_is_repeatable(db, migration):
    expected = snapshot(db)
    assert expected[0], "schema.sql에 표가 있어야 한다"
    db.execute("DROP TABLE conversation_scene_changes")
    db.execute(migration)
    db.execute(migration)
    assert snapshot(db) == expected


def test_chat_us_08_migration_aborts_on_unexpected_definition(db, migration):
    db.execute("DROP TABLE conversation_scene_changes")
    db.execute("CREATE TABLE conversation_scene_changes (id bigint PRIMARY KEY, note text)")
    with pytest.raises(psycopg.errors.RaiseException, match="unexpected columns"):
        db.execute(migration)
    db.execute("ROLLBACK")
    columns = [row["column_name"] for row in snapshot(db)[0]]
    assert columns == ["id", "note"]
