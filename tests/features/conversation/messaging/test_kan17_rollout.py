"""CHAT-US-02 배포 SQL: KAN-17 이전 DB에 적용·재실행해도 기존 방이 유지되고 정의가 같다."""

from pathlib import Path

from messaging_helpers import PLAYER, SESSIONS, send, user_part

MIGRATION = (
    Path(__file__).resolve().parents[4] / "app/db/migrations/kan17_conversation_messaging.sql"
)
KAN17_TABLES = ("conversation_sessions", "conversation_messages", "conversation_generations")


def schema_snapshot(db):
    """KAN-17이 다루는 표의 컬럼·제약·인덱스 정의."""
    columns = db.execute(
        """SELECT table_name, column_name, data_type, is_nullable, column_default,
                  is_identity
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


def drop_kan17_objects(db):
    """KAN-16까지만 적용된 기존 DB 상태로 되돌린다."""
    with db.transaction():
        db.execute("DROP TABLE conversation_generations, conversation_messages")
        db.execute("ALTER TABLE conversation_sessions DROP COLUMN model")


def test_chat_us_02_migration_on_legacy_db_keeps_rooms_and_matches_schema(db, client, session):
    expected = schema_snapshot(db)
    migration = MIGRATION.read_text(encoding="utf-8")
    drop_kan17_objects(db)
    try:
        before = db.execute(
            "SELECT * FROM conversation_sessions WHERE id = %s", (session,)
        ).fetchone()
        db.execute(migration)
        db.execute(migration)  # 재실행해도 같은 결과
        assert schema_snapshot(db) == expected
        after = db.execute(
            "SELECT * FROM conversation_sessions WHERE id = %s", (session,)
        ).fetchone()
        assert after == {**before, "model": None}

        room = client.get(f"{SESSIONS}/{session}", headers=PLAYER)
        assert room.status_code == 200
        assert room.json()["model"] is None
        assert send(client, session, [user_part("배포 뒤 첫 메시지")]).status_code == 202
        turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
        assert [m["role"] for m in turns["messages"]] == ["user", "assistant"]
        assert turns["model"] == "fake-echo"
    finally:
        # 중간에 실패해도 다음 테스트가 쓸 스키마를 남긴다.
        db.execute(migration)
