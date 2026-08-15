"""Central config loader. Reads from .env / environment variables.

See .env.example for the full list of variables and PLAN.md Section 4/6
for where these values and choices come from.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


# --- Discord ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = _get_int("DISCORD_GUILD_ID", 0)
VERIFY_CHANNEL_ID = _get_int("VERIFY_CHANNEL_ID", 0)
MOD_REVIEW_CHANNEL_ID = _get_int("MOD_REVIEW_CHANNEL_ID", 0)
VERIFIED_ROLE_ID = _get_int("VERIFIED_ROLE_ID", 0)

# --- Reddit ---
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME = os.getenv("REDDIT_USERNAME", "")
REDDIT_PASSWORD = os.getenv("REDDIT_PASSWORD", "")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "discord-verification-bot/1.0")
SUBREDDIT_NAME = os.getenv("SUBREDDIT_NAME", "")

# --- Shared DB ---
DB_PATH = os.getenv("DB_PATH", "verify.db")

# --- Thresholds (PLAN.md Section 4 — tunable) ---
MIN_ACCOUNT_AGE_DAYS = _get_int("MIN_ACCOUNT_AGE_DAYS", 30)
MIN_TOTAL_KARMA = _get_int("MIN_TOTAL_KARMA", 100)
MIN_SUBREDDIT_ACTIVITY_COUNT = _get_int("MIN_SUBREDDIT_ACTIVITY_COUNT", 5)
MIN_SUBREDDIT_KARMA = _get_int("MIN_SUBREDDIT_KARMA", 20)
CODE_EXPIRY_MINUTES = _get_int("CODE_EXPIRY_MINUTES", 30)
CODE_COOLDOWN_SECONDS = _get_int("CODE_COOLDOWN_SECONDS", 60)

# --- Polling ---
POLL_INTERVAL_SECONDS = _get_int("POLL_INTERVAL_SECONDS", 30)


def validate(require_discord: bool = False, require_reddit: bool = False) -> None:
    """Fail fast with a clear message instead of a confusing library traceback."""
    missing = []
    if require_discord:
        if not DISCORD_BOT_TOKEN:
            missing.append("DISCORD_BOT_TOKEN")
        if not VERIFY_CHANNEL_ID:
            missing.append("VERIFY_CHANNEL_ID")
    if require_reddit:
        for name in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"):
            if not globals()[name]:
                missing.append(name)
        if not SUBREDDIT_NAME:
            missing.append("SUBREDDIT_NAME")
    if missing:
        raise SystemExit(
            f"Missing required config: {', '.join(missing)}. "
            "Copy .env.example to .env and fill these in."
        )
