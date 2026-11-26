"""BASE-03 개발 DB 이관(base03_dev_owner_backfill.sql).

기존 owner_id마다 같은 id의 active 계정과 dev 로그인 연결을 만들어, dev 키 "1"로 로그인하면
예전 X-User-Id: 1의 데이터가 보인다. 허용 목록에 없는 DB 이름에서는 아무것도 바꾸지 않고 중단한다.
"""

from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from tests.features.account.account_helpers import (
    STORIES,
    bearer,
    dev_login,
    seed_conversation_session,
    signup,
)

ROOT = Path(__file__).resolve().parents[4]
BACKFILL = ROOT / "app/db/migrations/base03_dev_owner_backfill.sql"
MIGRATION = ROOT / "app/db/migrations/base03_accounts.sql"
OWNER_TABLE_ROWS = {
    "stories": "INSERT INTO stories (owner_id, title) VALUES (1, 'a'), (3, 'b')",
    "images": "INSERT INTO images (owner_id, storage_key, mime_type, byte_size) "
    "VALUES (5, 'k5', 'image/png', 1)",
    "user_play_settings": "INSERT INTO user_play_settings (owner_id, default_play_name) "
    "VALUES (8, '팔')",
    "creation_write_requests": "INSERT INTO creation_write_requests "
    "(owner_id, request_id, story_id, request_hash, response) "
    "SELECT 13, 'r', min(id), 'h', '{}' FROM stories",
    "user_profiles": "INSERT INTO user_profiles (owner_id, name, image_id) "
    "SELECT 21, 'p', min(id) FROM images",
}
OWNER_IDS = (1, 3, 5, 8, 13, 21, 34)


def seed_owner_data(db) -> None:
    for statement in OWNER_TABLE_ROWS.values():
        db.execute(statement)
    seed_conversation_session(db, 34)  # 34의 스토리·이미지·프로필·대화 세션


def test_backfill_creates_active_dev_accounts_for_existing_owners_BASE_03(db, client):
    seed_owner_data(db)
    script = BACKFILL.read_text(encoding="utf-8")
    db.execute(script)
    db.execute(script)  # 재실행해도 같다

    users = db.execute("SELECT id, status FROM users ORDER BY id").fetchall()
    assert users == [{"id": i, "status": "active"} for i in OWNER_IDS]
    identities = db.execute(
        "SELECT user_id, provider, provider_user_id FROM user_identities ORDER BY user_id"
    ).fetchall()
    assert identities == [
        {"user_id": i, "provider": "dev", "provider_user_id": str(i)} for i in OWNER_IDS
    ]

    # dev 키 "1"은 예전 owner 1의 데이터를 그대로 본다.
    old_owner = dev_login(client, "1")
    assert old_owner["user"]["user_id"] == 1
    assert old_owner["user"]["status"] == "active"
    listed = client.get(STORIES, headers=bearer(old_owner["access_token"])).json()
    assert [story["title"] for story in listed] == ["a"]
    # 새 키는 옮긴 id와 겹치지 않는 새 계정이 된다(시퀀스 보정).
    newcomer = dev_login(client, "brand-new")
    assert newcomer["user"]["user_id"] == max(OWNER_IDS) + 1
    assert newcomer["user"]["status"] == "pending"


def test_backfill_after_migration_keeps_users_sequence_forward_BASE_03(db, client):
    """문서 순서(마이그레이션 → 이관)대로 돌리면 이관이 시퀀스를 1000 아래로 되돌리지 않는다."""
    seed_owner_data(db)
    db.execute(MIGRATION.read_text(encoding="utf-8"))  # 기존 owner_id가 있어도 1000부터
    db.execute(BACKFILL.read_text(encoding="utf-8"))
    newcomer = dev_login(client, "brand-new")
    assert newcomer["user"]["user_id"] == 1000
    assert dev_login(client, "1")["user"]["user_id"] == 1  # 이관 계정은 그대로


def test_backfill_keeps_existing_accounts_BASE_03(db, client):
    taken = dev_login(client, "someone")  # id 1, pending
    seed_owner_data(db)
    db.execute(BACKFILL.read_text(encoding="utf-8"))
    row = db.execute("SELECT status FROM users WHERE id = %s", (taken["user"]["user_id"],))
    assert row.fetchone()["status"] == "pending"
    providers = db.execute(
        "SELECT provider_user_id FROM user_identities WHERE user_id = 1 ORDER BY 1"
    ).fetchall()
    assert [p["provider_user_id"] for p in providers] == ["1", "someone"]


def test_backfill_gives_no_dev_key_to_email_accounts_BASE_03(db, client):
    """이메일 계정의 데이터가 있어도 이관이 그 계정에 dev 로그인 키를 붙이지 않는다."""
    member = signup(client, "member@example.com").json()
    member_id = member["user"]["user_id"]
    db.execute("INSERT INTO stories (owner_id, title) VALUES (%s, 'mine')", (member_id,))
    seed_owner_data(db)
    db.execute(BACKFILL.read_text(encoding="utf-8"))
    rows = db.execute(
        "SELECT provider_user_id FROM user_identities WHERE user_id = %s", (member_id,)
    ).fetchall()
    assert rows == []
    other = dev_login(client, str(member_id))
    assert other["user"]["user_id"] != member_id


@pytest.fixture
def probe_db(db_url):
    """허용 목록에 없는 이름의 임시 DB(같은 폐기용 테스트 서버). 테스트 뒤 지운다."""
    name = conninfo_to_dict(db_url)["dbname"] + "_probe"
    admin_url = make_conninfo(db_url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(db_url, dbname=name)
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute((ROOT / "app/db/schema.sql").read_text(encoding="utf-8"))
        yield name, conn
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def test_backfill_refuses_non_allow_listed_database_BASE_03(probe_db):
    name, conn = probe_db
    conn.execute("INSERT INTO stories (owner_id, title) VALUES (1, 'a')")
    with pytest.raises(psycopg.errors.RaiseException, match="not an allow-listed dev database"):
        conn.execute(BACKFILL.read_text(encoding="utf-8"))
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0


def test_backfill_runs_on_explicitly_named_local_database_BASE_03(probe_db):
    """로컬 Docker 기본 DB처럼 접미어가 없는 DB는 운영자가 이름을 직접 적어야 실행된다."""
    name, conn = probe_db
    conn.execute("INSERT INTO stories (owner_id, title) VALUES (2, 'a')")
    conn.execute("SELECT set_config('charchat.dev_backfill_database', 'other_db', false)")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute(BACKFILL.read_text(encoding="utf-8"))
    conn.execute("ROLLBACK")
    conn.execute("SELECT set_config('charchat.dev_backfill_database', %s, false)", (name,))
    conn.execute(BACKFILL.read_text(encoding="utf-8"))
    assert conn.execute("SELECT id, status FROM users").fetchall() == [
        {"id": 2, "status": "active"}
    ]


def test_backfill_requires_base03_tables_BASE_03(db):
    db.execute(
        "DROP TABLE auth_login_attempts, auth_sessions, user_password_credentials, "
        "user_consents, user_identities, users"
    )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="apply base03_accounts.sql first"):
            db.execute(BACKFILL.read_text(encoding="utf-8"))
        db.execute("ROLLBACK")
    finally:
        db.execute(MIGRATION.read_text(encoding="utf-8"))
