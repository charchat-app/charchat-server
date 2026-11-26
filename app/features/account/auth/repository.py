"""BASE-03 로그인 계정·로그인 세션 DB 접근. 토큰 원문은 다루지 않고 sha256 해시만 쓴다."""

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


def create_session(
    conn: Connection,
    user_id: int,
    client_platform: str,
    access_hash: bytes,
    refresh_hash: bytes,
    access_ttl_seconds: int,
    refresh_ttl_days: int,
) -> int:
    return conn.execute(
        """INSERT INTO auth_sessions (user_id, client_platform, access_token_hash,
                                      access_expires_at, refresh_token_hash, refresh_expires_at)
           VALUES (%s, %s, %s, clock_timestamp() + make_interval(secs => %s), %s,
                   clock_timestamp() + make_interval(days => %s))
           RETURNING id""",
        (user_id, client_platform, access_hash, access_ttl_seconds, refresh_hash, refresh_ttl_days),
    ).fetchone()["id"]


def find_session_by_access(conn: Connection, access_hash: bytes):
    return conn.execute(
        """SELECT s.id, s.user_id, u.status,
                  s.revoked_at IS NOT NULL AS revoked,
                  s.access_expires_at <= clock_timestamp() AS expired,
                  s.last_used_at < clock_timestamp() - interval '1 minute' AS stale
           FROM auth_sessions s JOIN users u ON u.id = s.user_id
           WHERE s.access_token_hash = %s""",
        (access_hash,),
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
    conn: Connection,
    session_id: int,
    access_hash: bytes,
    refresh_hash: bytes,
    access_ttl_seconds: int,
    refresh_ttl_days: int,
) -> None:
    conn.execute(
        """UPDATE auth_sessions SET
               previous_refresh_hash = refresh_token_hash,
               refresh_token_hash = %s,
               refresh_expires_at = clock_timestamp() + make_interval(days => %s),
               access_token_hash = %s,
               access_expires_at = clock_timestamp() + make_interval(secs => %s),
               rotated_at = clock_timestamp(),
               last_used_at = clock_timestamp()
           WHERE id = %s""",
        (refresh_hash, refresh_ttl_days, access_hash, access_ttl_seconds, session_id),
    )


def revoke_session(conn: Connection, session_id: int, reason: str) -> None:
    conn.execute(
        """UPDATE auth_sessions SET revoked_at = clock_timestamp(), revoke_reason = %s
           WHERE id = %s AND revoked_at IS NULL""",
        (reason, session_id),
    )
