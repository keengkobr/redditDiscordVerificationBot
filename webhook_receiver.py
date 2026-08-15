"""Process 2 (DEVVIT_PIVOT_SPEC.md): receives verification verdicts computed by
the Devvit app (Reddit-hosted, see devvit/) and writes them into the shared
verify.db -- exactly what reddit_poller.py used to do, minus the inbox
polling. Retires reddit_poller.py entirely: this is purely event-driven, only
running when the Devvit app's form-submit handler POSTs a result.

Never talks to discord_bot.py directly -- only through verify.db, same as
every other process in this project.
"""

import hmac
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import config
import db

app = FastAPI()


class VerdictPayload(BaseModel):
    code: str
    reddit_username: str
    status: str = Field(pattern="^(verified|failed)$")
    fail_reason: Optional[str] = None
    account_age_days: Optional[int] = None
    total_karma: Optional[int] = None
    subreddit_activity_count: Optional[int] = None
    subreddit_karma: Optional[int] = None


@app.on_event("startup")
def _startup() -> None:
    config.validate(require_webhook=True)
    db.init_db(config.DB_PATH)


def _check_secret(secret: Optional[str]) -> None:
    # constant-time comparison -- this header is the one thing standing between
    # the public internet and writes into verify.db.
    if not secret or not config.DEVVIT_WEBHOOK_SECRET or not hmac.compare_digest(
        secret, config.DEVVIT_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=401, detail="invalid or missing secret")


@app.post("/devvit/verdict")
def receive_verdict(
    payload: VerdictPayload,
    x_devvit_secret: Optional[str] = Header(default=None),
):
    _check_secret(x_devvit_secret)

    conn = db.connect(config.DB_PATH)
    try:
        pending = db.get_pending_by_code(conn, payload.code)
        if not pending:
            # Unknown, already-resolved, or never-existed code. 400 rather than
            # 404 -- from the Devvit app's perspective this is a bad request,
            # not a missing resource.
            raise HTTPException(status_code=400, detail="unknown or already-resolved code")

        if time.time() - pending["code_created_at"] > config.CODE_EXPIRY_MINUTES * 60:
            db.set_result(conn, pending["id"], "failed", payload.reddit_username, "code_expired")
            conn.commit()
            raise HTTPException(status_code=400, detail="code expired")

        if payload.status == "verified" and db.is_reddit_username_taken(
            conn, payload.reddit_username, exclude_id=pending["id"]
        ):
            db.set_result(
                conn, pending["id"], "failed", payload.reddit_username, "reddit_account_already_linked"
            )
            conn.commit()
            raise HTTPException(
                status_code=409, detail="reddit account already linked to a different Discord account"
            )

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
        print(f"[webhook_receiver] {payload.status} u/{payload.reddit_username} (code {payload.code})")
        return {"ok": True}
    finally:
        conn.close()
