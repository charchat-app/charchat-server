"""BASE-03 내 계정 조회·온보딩 DB 접근."""

from psycopg import Connection


def get_user(conn: Connection, user_id: int, *, lock: bool = False):
    query = "SELECT id, status FROM users WHERE id = %s AND status <> 'deleted'"
    if lock:
        query += " FOR UPDATE"
    return conn.execute(query, (user_id,)).fetchone()


def list_providers(conn: Connection, user_id: int) -> list[str]:
    """연결된 로그인 수단. 이메일 로그인 정보가 있으면 'email'을 넣는다."""
    rows = conn.execute(
        """SELECT provider FROM user_identities WHERE user_id = %s
           UNION
           SELECT 'email' FROM user_password_credentials WHERE user_id = %s
           ORDER BY provider""",
        (user_id, user_id),
    ).fetchall()
    return [row["provider"] for row in rows]


def get_default_play_name(conn: Connection, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT default_play_name FROM user_play_settings WHERE owner_id = %s", (user_id,)
    ).fetchone()
    return row["default_play_name"] if row else None


def latest_consents(conn: Connection, user_id: int) -> dict[str, str]:
    rows = conn.execute(
        """SELECT DISTINCT ON (kind) kind, version FROM user_consents
           WHERE user_id = %s ORDER BY kind, agreed_at DESC, version DESC""",
        (user_id,),
    ).fetchall()
    return {row["kind"]: row["version"] for row in rows}


def has_consents(conn: Connection, user_id: int, required: dict[str, str]) -> bool:
    row = conn.execute(
        """SELECT count(*) AS n FROM user_consents c
           JOIN unnest(%s::text[], %s::text[]) AS r(kind, version)
             ON r.kind = c.kind AND r.version = c.version
           WHERE c.user_id = %s""",
        (list(required), list(required.values()), user_id),
    ).fetchone()
    return row["n"] == len(required)


def add_consent(conn: Connection, user_id: int, kind: str, version: str) -> None:
    conn.execute(
        """INSERT INTO user_consents (user_id, kind, version) VALUES (%s, %s, %s)
           ON CONFLICT (user_id, kind, version) DO NOTHING""",
        (user_id, kind, version),
    )


def upsert_default_play_name(conn: Connection, user_id: int, name: str) -> None:
    conn.execute(
        """INSERT INTO user_play_settings (owner_id, default_play_name) VALUES (%s, %s)
           ON CONFLICT (owner_id) DO UPDATE SET default_play_name = EXCLUDED.default_play_name""",
        (user_id, name),
    )


def activate_user(conn: Connection, user_id: int) -> None:
    conn.execute(
        """UPDATE users SET status = 'active', onboarded_at = clock_timestamp()
           WHERE id = %s AND status = 'pending'""",
        (user_id,),
    )
