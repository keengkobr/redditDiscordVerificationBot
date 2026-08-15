"""Process 2 (PLAN.md Section 2/3): polls the bot account's Reddit inbox for
verification codes, checks account age/karma/subreddit activity via PRAW,
and writes the result to the shared SQLite database.

Never talks to discord_bot.py directly — only through verify.db.
"""

import re
import time

import praw
import prawcore

import config
import db

CODE_RE = re.compile(r"\b([A-Z0-9]{6})\b")


def build_reddit() -> praw.Reddit:
    return praw.Reddit(
        client_id=config.REDDIT_CLIENT_ID,
        client_secret=config.REDDIT_CLIENT_SECRET,
        username=config.REDDIT_USERNAME,
        password=config.REDDIT_PASSWORD,
        user_agent=config.REDDIT_USER_AGENT,
    )


def extract_code(body: str):
    match = CODE_RE.search((body or "").upper())
    return match.group(1) if match else None


def check_thresholds(reddit: praw.Reddit, username: str):
    """Run the checks from PLAN.md Section 4.

    Returns (passed: bool, fail_reason: str | None).
    fail_reason == "no_visible_activity" is a *soft fail* (PLAN.md Section 11)
    and should be routed to mod review rather than treated as a hard reject —
    it usually means a curated/hidden profile, not a burner account.
    """
    redditor = reddit.redditor(username)
    try:
        created_utc = redditor.created_utc
        link_karma = redditor.link_karma
        comment_karma = redditor.comment_karma
    except (prawcore.exceptions.NotFound, prawcore.exceptions.Forbidden):
        return False, "reddit_account_not_found"

    account_age_days = (time.time() - created_utc) / 86400
    total_karma = link_karma + comment_karma

    sub_name = config.SUBREDDIT_NAME.lower()
    sub_count = 0
    sub_karma = 0
    found_any_history = False

    try:
        for comment in redditor.comments.new(limit=200):
            found_any_history = True
            if str(comment.subreddit).lower() == sub_name:
                sub_count += 1
                sub_karma += comment.score
        for submission in redditor.submissions.new(limit=200):
            found_any_history = True
            if str(submission.subreddit).lower() == sub_name:
                sub_count += 1
                sub_karma += submission.score
    except (prawcore.exceptions.Forbidden, prawcore.exceptions.NotFound):
        # Suspended/shadowbanned or otherwise inaccessible — treat as no history found.
        pass

    reasons = []
    if account_age_days < config.MIN_ACCOUNT_AGE_DAYS:
        reasons.append(f"account_age:{account_age_days:.0f}d<{config.MIN_ACCOUNT_AGE_DAYS}d")
    if total_karma < config.MIN_TOTAL_KARMA:
        reasons.append(f"total_karma:{total_karma}<{config.MIN_TOTAL_KARMA}")
    if sub_count < config.MIN_SUBREDDIT_ACTIVITY_COUNT:
        reasons.append(f"subreddit_activity:{sub_count}<{config.MIN_SUBREDDIT_ACTIVITY_COUNT}")
    if sub_karma < config.MIN_SUBREDDIT_KARMA:
        reasons.append(f"subreddit_karma:{sub_karma}<{config.MIN_SUBREDDIT_KARMA}")

    if not reasons:
        return True, None

    # Zero subreddit activity visible at all, despite an account old enough to plausibly
    # have some, is the classic curated/hidden-profile false negative (Section 11) —
    # soft-fail it instead of hard-rejecting.
    if sub_count == 0 and account_age_days >= config.MIN_ACCOUNT_AGE_DAYS:
        return False, "no_visible_activity"

    return False, ";".join(reasons)


def process_inbox(reddit: praw.Reddit, conn) -> None:
    for message in reddit.inbox.unread(limit=50):
        try:
            if not isinstance(message, praw.models.Message):
                message.mark_read()
                continue

            code = extract_code(message.body)
            author = str(message.author) if message.author else None
            message.mark_read()

            if not code or not author:
                continue

            pending = db.get_pending_by_code(conn, code)
            if not pending:
                continue  # Unknown, already-consumed, or expired code — ignore silently.

            if time.time() - pending["code_created_at"] > config.CODE_EXPIRY_MINUTES * 60:
                db.set_result(conn, pending["id"], "failed", author, "code_expired")
                continue

            if db.is_reddit_username_taken(conn, author, exclude_id=pending["id"]):
                db.set_result(conn, pending["id"], "failed", author, "reddit_account_already_linked")
                continue

            passed, fail_reason = check_thresholds(reddit, author)
            if passed:
                db.set_result(conn, pending["id"], "verified", author, None)
                print(f"[reddit_poller] verified u/{author} -> discord {pending['discord_user_id']}")
            else:
                db.set_result(conn, pending["id"], "failed", author, fail_reason)
                print(f"[reddit_poller] failed u/{author}: {fail_reason}")

        except Exception as exc:  # noqa: BLE001 - keep the loop alive no matter what
            print(f"[reddit_poller] error processing message {getattr(message, 'id', '?')}: {exc}")


def main() -> None:
    config.validate(require_reddit=True)
    db.init_db(config.DB_PATH)
    reddit = build_reddit()
    print(f"[reddit_poller] logged in as u/{reddit.user.me()}, polling every {config.POLL_INTERVAL_SECONDS}s")

    while True:
        conn = db.connect(config.DB_PATH)
        try:
            process_inbox(reddit, conn)
            expired = db.expire_old_pending(conn, config.CODE_EXPIRY_MINUTES * 60)
            conn.commit()
            if expired:
                print(f"[reddit_poller] expired {expired} stale pending code(s)")
        except Exception as exc:  # noqa: BLE001 - never let a transient API error kill the loop
            print(f"[reddit_poller] loop error: {exc}")
        finally:
            conn.close()
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
