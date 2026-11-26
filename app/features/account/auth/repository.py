"""BASE-03 로그인 계정·이메일 로그인 정보·로그인 세션 DB 접근.

리프레시 토큰은 sha256 해시만, 비밀번호는 bcrypt 해시만 다룬다. 액세스 토큰(JWT)은 저장하지 않는다.
"""

from psycopg import Connection


def find_identity(conn: Connection, provider: str, subject: str):
    return conn.execute(
        """SELECT i.id, i.user_id, u.status FROM user_identities i
           JOIN users u ON u.id = i.user_id
           WHERE i.provider = %s AND i.provider_user_id = %s""",
        (provider, subject),
    ).fetchone()


def touch_identity(conn: Connection, identity_id: int) -> None:
    conn.execute(
        "UPDATE user_identities SET last_login_at = clock_timestamp() WHERE id = %s",
        (identity_id,),
    )


def create_pending_user(conn: Connection) -> int:
    return conn.execute("INSERT INTO users DEFAULT VALUES RETURNING id").fetchone()["id"]


def delete_user(conn: Connection, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = %s", (user_id,))


def create_identity(conn: Connection, user_id: int, provider: str, subject: str) -> bool:
    """새 연결을 만들면 True. 같은 (provider, subject)가 동시에 먼저 만들어졌으면 False."""
    row = conn.execute(
        """INSERT INTO user_identities (user_id, provider, provider_user_id) VALUES (%s, %s, %s)
           ON CONFLICT (provider, provider_user_id) DO NOTHING RETURNING id""",
        (user_id, provider, subject),
    ).fetchone()
    return row is not None


def create_password_credential(
    conn: Connection, user_id: int, email: str, password_hash: str
) -> bool:
    """이메일 로그인 정보를 만든다. 같은 이메일이 이미(또는 동시에 먼저) 있으면 False."""
    row = conn.execute(
        """INSERT INTO user_password_credentials (user_id, email, password_hash)
           VALUES (%s, %s, %s)
           ON CONFLICT (email) DO NOTHING RETURNING user_id""",
        (user_id, email, password_hash),
    ).fetchone()
    return row is not None


def find_password_credential(conn: Connection, email: str):
    return conn.execute(
        """SELECT c.user_id, c.password_hash, u.status FROM user_password_credentials c
           JOIN users u ON u.id = c.user_id
           WHERE c.email = %s""",
        (email,),
    ).fetchone()


def create_session(
    conn: Connection,
    user_id: int,
    client_platform: str,
    refresh_hash: bytes,
    refresh_ttl_days: int,
) -> int:
    return conn.execute(
        """INSERT INTO auth_sessions (user_id, client_platform, refresh_token_hash,
                                      refresh_expires_at)
           VALUES (%s, %s, %s, clock_timestamp() + make_interval(days => %s))
           RETURNING id""",
        (user_id, client_platform, refresh_hash, refresh_ttl_days),
    ).fetchone()["id"]


def find_session(conn: Connection, session_id: int):
    """액세스 토큰(JWT)의 sid로 세션과 계정 상태를 한 번에 읽는다."""
    return conn.execute(
        """SELECT s.id, s.user_id, u.status,
                  s.revoked_at IS NOT NULL AS revoked,
                  s.last_used_at < clock_timestamp() - interval '1 minute' AS stale
           FROM auth_sessions s JOIN users u ON u.id = s.user_id
           WHERE s.id = %s""",
        (session_id,),
    ).fetchone()


def touch_session(conn: Connection, session_id: int) -> None:
    conn.execute(
        "UPDATE auth_sessions SET last_used_at = clock_timestamp() WHERE id = %s", (session_id,)
    )


def lock_session_by_refresh(conn: Connection, refresh_hash: bytes, grace_seconds: float):
    """현재 또는 직전 리프레시 토큰으로 세션 행을 잠근다. 회전과 동시에 오면 갱신된 행을 본다."""
    return conn.execute(
        """SELECT s.id, s.user_id, s.client_platform, u.status,
                  s.revoked_at IS NOT NULL AS revoked,
                  s.refresh_token_hash = %s AS is_current,
                  s.refresh_expires_at <= clock_timestamp() AS refresh_expired,
                  s.rotated_at IS NOT NULL
                      AND s.rotated_at > clock_timestamp() - make_interval(secs => %s)
                      AS within_grace
           FROM auth_sessions s JOIN users u ON u.id = s.user_id
           WHERE s.refresh_token_hash = %s OR s.previous_refresh_hash = %s
           FOR UPDATE OF s""",
        (refresh_hash, grace_seconds, refresh_hash, refresh_hash),
    ).fetchone()


def rotate_session(
    conn: Connection, session_id: int, refresh_hash: bytes, refresh_ttl_days: int
) -> None:
    conn.execute(
        """UPDATE auth_sessions SET
               previous_refresh_hash = refresh_token_hash,
               refresh_token_hash = %s,
               refresh_expires_at = clock_timestamp() + make_interval(days => %s),
               rotated_at = clock_timestamp(),
               last_used_at = clock_timestamp()
           WHERE id = %s""",
        (refresh_hash, refresh_ttl_days, session_id),
    )


def revoke_session(conn: Connection, session_id: int, reason: str) -> None:
    conn.execute(
        """UPDATE auth_sessions SET revoked_at = clock_timestamp(), revoke_reason = %s
           WHERE id = %s AND revoked_at IS NULL""",
        (reason, session_id),
    )
