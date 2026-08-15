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
    account_age_days INTEGER,
    total_karma INTEGER,
    subreddit_activity_count INTEGER,
    subreddit_karma INTEGER,
    logged_to_discord INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_discord_user ON verifications(discord_user_id);
CREATE INDEX IF NOT EXISTS idx_code ON verifications(code);
CREATE INDEX IF NOT EXISTS idx_status_processed ON verifications(status, processed_at);
"""
# idx_status_logged and idx_unique_verified_reddit_username are created in _migrate(),
# not here: on an existing (pre-this-feature) database the logged_to_discord column
# doesn't exist until _migrate() adds it, and this script runs via executescript()
# *before* _migrate() — referencing that column here would fail with "no such column"
# on any already-deployed verify.db.
#
# The reddit_username uniqueness constraint is a PARTIAL index scoped to
# status='verified', not a table-wide UNIQUE(reddit_username) column constraint.
# One Reddit account per Discord account (PLAN.md Section 5) only needs to hold
# among *active, verified* claims -- a table-wide constraint also blocked writing
# a second 'failed' row for a username that's verified elsewhere (or even two
# 'failed' rows sharing a username), which raised IntegrityError on a legitimate,
# expected case: a user retrying verification with the same Reddit account.

# Columns added after the initial release. Applied via PRAGMA-checked ALTER TABLE
# so existing verify.db files on already-deployed VPSes upgrade in place — the
# CREATE TABLE above only takes effect for brand-new databases.
_MIGRATION_COLUMNS = {
    "account_age_days": "INTEGER",
    "total_karma": "INTEGER",
    "subreddit_activity_count": "INTEGER",
    "subreddit_karma": "INTEGER",
    "logged_to_discord": "INTEGER DEFAULT 0",
}


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _has_table_wide_reddit_username_unique(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='verifications'"
    ).fetchone()
    return bool(row and row["sql"] and "UNIQUE(reddit_username)" in row["sql"].replace(" ", "")
                .replace("\n", ""))


def _rebuild_table_without_column_unique(conn: sqlite3.Connection) -> None:
    """SQLite can't ALTER TABLE to drop a column-level UNIQUE constraint --
    only a full rebuild does it. Only reached for a verify.db created before
    this fix; new databases never hit this path (see SCHEMA above).
    """
    conn.execute("ALTER TABLE verifications RENAME TO verifications_old")
    conn.executescript(SCHEMA)
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(verifications_old)")]
    common = ", ".join(c for c in columns if c != "id")
    conn.execute(
        f"INSERT INTO verifications (id, {common}) SELECT id, {common} FROM verifications_old"
    )
    conn.execute("DROP TABLE verifications_old")


def _migrate(conn: sqlite3.Connection) -> None:
    if _has_table_wide_reddit_username_unique(conn):
        _rebuild_table_without_column_unique(conn)

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(verifications)")}
    for column, declaration in _MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE verifications ADD COLUMN {column} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_logged ON verifications(status, logged_to_discord)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_verified_reddit_username "
        "ON verifications(reddit_username) WHERE status = 'verified'"
    )


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
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


def set_result(
    conn,
    verification_id: int,
    status: str,
    reddit_username,
    fail_reason,
    account_age_days=None,
    total_karma=None,
    subreddit_activity_count=None,
    subreddit_karma=None,
) -> None:
    """Record a verdict. The four metric args are the raw numbers the verdict was
    based on (VerificationLogChannel.md) — pass them whenever check_thresholds()
    actually ran (i.e. for every pass/fail past reddit_account_not_found), so the
    log embed can show real numbers instead of a bare category string.
    """
    now = int(time.time())
    verified_at = now if status == "verified" else None
    conn.execute(
        "UPDATE verifications SET status = ?, reddit_username = ?, fail_reason = ?, "
        "verified_at = ?, account_age_days = ?, total_karma = ?, "
        "subreddit_activity_count = ?, subreddit_karma = ? WHERE id = ?",
        (
            status,
            reddit_username,
            fail_reason,
            verified_at,
            account_age_days,
            total_karma,
            subreddit_activity_count,
            subreddit_karma,
            verification_id,
        ),
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


def get_unlogged_results(conn):
    """Verdicts not yet posted to the Discord log channel. Tracked independently
    of processed_at so a crash between role/DM handling and log-posting doesn't
    lose the log entry — it's picked up again on the next poll/restart regardless
    of whether processed_at was already set.
    """
    return conn.execute(
        "SELECT * FROM verifications WHERE status IN ('verified', 'failed') "
        "AND logged_to_discord = 0 ORDER BY id ASC"
    ).fetchall()


def mark_logged(conn, verification_id: int) -> None:
    conn.execute(
        "UPDATE verifications SET logged_to_discord = 1 WHERE id = ?",
        (verification_id,),
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
