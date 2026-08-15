"""Shared verdict-processing logic (DEVVIT_PIVOT_SPEC.md v4).

Reddit's HTTP Fetch Policy explicitly states personal domains are never
approved -- only a fixed global allowlist (which includes discord.com) skips
review. That makes a self-hosted webhook_receiver.py on a custom domain a
dead end. This module replaces it: the Devvit app POSTs its verdict to a
Discord Incoming Webhook instead, discord_bot.py's on_message handler reads
that channel and calls process_verdict() directly -- no HTTP server, no
domain, no nginx/TLS needed for this leg at all.

Same DB semantics as the retired webhook_receiver.py, just a different
transport: discord_bot.py already has an open connection and the same
verify.db, so this is a plain function call, not an HTTP round-trip.
"""

import time
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

import config
import db


class VerdictPayload(BaseModel):
    code: str
    reddit_username: str
    status: str = Field(pattern="^(verified|failed)$")
    fail_reason: Optional[str] = None
    account_age_days: Optional[int] = None
    total_karma: Optional[int] = None
    subreddit_activity_count: Optional[int] = None
    subreddit_karma: Optional[int] = None


class VerdictError(Exception):
    """Raised for any rejected/invalid verdict -- callers decide how to log it."""


def process_verdict(raw: dict) -> None:
    """Validates and writes a verdict into verify.db. Raises VerdictError on
    anything rejected (unknown/expired/already-resolved code, username
    conflict, malformed payload) -- mirrors webhook_receiver.py's HTTP status
    codes conceptually (400/409), just without an HTTP layer to attach them to.
    """
    try:
        payload = VerdictPayload(**raw)
    except ValidationError as exc:
        raise VerdictError(f"invalid payload: {exc}") from exc

    conn = db.connect(config.DB_PATH)
    try:
        pending = db.get_pending_by_code(conn, payload.code)
        if not pending:
            raise VerdictError("unknown or already-resolved code")

        if time.time() - pending["code_created_at"] > config.CODE_EXPIRY_MINUTES * 60:
            db.set_result(conn, pending["id"], "failed", payload.reddit_username, "code_expired")
            conn.commit()
            raise VerdictError("code expired")

        if payload.status == "verified" and db.is_reddit_username_taken(
            conn, payload.reddit_username, exclude_id=pending["id"]
        ):
            db.set_result(
                conn, pending["id"], "failed", payload.reddit_username, "reddit_account_already_linked"
            )
            conn.commit()
            raise VerdictError("reddit account already linked to a different Discord account")

        db.set_result(
            conn,
            pending["id"],
            payload.status,
            payload.reddit_username,
            payload.fail_reason,
            account_age_days=payload.account_age_days,
            total_karma=payload.total_karma,
            subreddit_activity_count=payload.subreddit_activity_count,
            subreddit_karma=payload.subreddit_karma,
        )
        conn.commit()
    finally:
        conn.close()
