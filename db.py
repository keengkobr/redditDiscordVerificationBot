"""Shared SQLite data-access layer (PLAN.md Section 5).

Both discord_bot.py and reddit_poller.py talk to each other only through
this database — there is no direct process-to-process communication.

Plain sqlite3 is used (not aiosqlite) so both a sync process (the poller)
and an async process (the bot, via asyncio.to_thread) can share identical
logic. WAL mode + a busy timeout let the two processes write concurrently
without stepping on each other.
"""

import secrets
import sqlite3
import string
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id TEXT NOT NULL,
    reddit_username TEXT,
    status TEXT CHECK(status IN ('pending','verified','failed','revoked')) NOT NULL,
    code TEXT NOT NULL,
    code_created_at INTEGER NOT NULL,
    verified_at INTEGER,
    fail_reason TEXT,
    processed_at INTEGER,
    UNIQUE(reddit_username)
);

CREATE INDEX IF NOT EXISTS idx_discord_user ON verifications(discord_user_id);
CREATE INDEX IF NOT EXISTS idx_code ON verifications(code);
CREATE INDEX IF NOT EXISTS idx_status_processed ON verifications(status, processed_at);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def generate_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_or_get_pending(conn, discord_user_id: str, expiry_seconds: int, cooldown_seconds: int) -> dict:
    """Idempotently get a usable code for this Discord user.

    Returns one of:
      {"state": "already_verified"}
      {"state": "reused", "code": "..."}      -- existing, still-valid code
      {"state": "new", "code": "..."}         -- freshly generated
    """
    now = int(time.time())

    verified = conn.execute(
        "SELECT id FROM verifications WHERE discord_user_id = ? AND status = 'verified'",
        (discord_user_id,),
    ).fetchone()
    if verified:
        return {"state": "already_verified"}

    pending = conn.execute(
        "SELECT * FROM verifications WHERE discord_user_id = ? AND status = 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (discord_user_id,),
    ).fetchone()

    if pending:
        age = now - pending["code_created_at"]
        if age < expiry_seconds:
            # Still valid — reuse it. (Also naturally rate-limits repeated clicks:
            # cooldown_seconds is enforced by the caller deciding whether to re-DM.)
            return {
                "state": "reused",
                "code": pending["code"],
                "rate_limited": age < cooldown_seconds,
            }
        # Expired — recycle the row with a fresh code instead of leaving stale rows around.
        new_code = generate_code()
        conn.execute(
            "UPDATE verifications SET code = ?, code_created_at = ? WHERE id = ?",
            (new_code, now, pending["id"]),
        )
        return {"state": "new", "code": new_code}

    new_code = generate_code()
    conn.execute(
        "INSERT INTO verifications (discord_user_id, status, code, code_created_at) "
        "VALUES (?, 'pending', ?, ?)",
        (discord_user_id, new_code, now),
    )
    return {"state": "new", "code": new_code}


def get_pending_by_code(conn, code: str):
    return conn.execute(
        "SELECT * FROM verifications WHERE code = ? AND status = 'pending'", (code,)
    ).fetchone()


def is_reddit_username_taken(conn, reddit_username: str, exclude_id=None) -> bool:
    row = conn.execute(
        "SELECT id FROM verifications WHERE reddit_username = ? AND status = 'verified' "
        "AND id != ?",
        (reddit_username, exclude_id if exclude_id is not None else -1),
    ).fetchone()
    return row is not None


def set_result(conn, verification_id: int, status: str, reddit_username, fail_reason) -> None:
    now = int(time.time())
    verified_at = now if status == "verified" else None
    conn.execute(
        "UPDATE verifications SET status = ?, reddit_username = ?, fail_reason = ?, "
        "verified_at = ? WHERE id = ?",
        (status, reddit_username, fail_reason, verified_at, verification_id),
    )


def expire_old_pending(conn, expiry_seconds: int) -> int:
    """Mark stale pending codes as failed/expired. Returns count affected."""
    cutoff = int(time.time()) - expiry_seconds
    now = int(time.time())
    cur = conn.execute(
        "UPDATE verifications SET status = 'failed', fail_reason = 'code_expired', "
        "processed_at = ? WHERE status = 'pending' AND code_created_at < ?",
        (now, cutoff),
    )
    return cur.rowcount


def get_unprocessed_results(conn):
    return conn.execute(
        "SELECT * FROM verifications WHERE status IN ('verified', 'failed') "
        "AND processed_at IS NULL ORDER BY id ASC"
    ).fetchall()


def mark_processed(conn, verification_id: int) -> None:
    conn.execute(
        "UPDATE verifications SET processed_at = ? WHERE id = ?",
        (int(time.time()), verification_id),
    )


def get_verification_by_id(conn, verification_id: int):
    return conn.execute(
        "SELECT * FROM verifications WHERE id = ?", (verification_id,)
    ).fetchone()


def get_verification_for_discord_user(conn, discord_user_id: str):
    return conn.execute(
        "SELECT * FROM verifications WHERE discord_user_id = ? ORDER BY id DESC LIMIT 1",
        (discord_user_id,),
    ).fetchone()


def revoke(conn, discord_user_id: str) -> None:
    conn.execute(
        "UPDATE verifications SET status = 'revoked' WHERE discord_user_id = ? "
        "AND status = 'verified'",
        (discord_user_id,),
    )
