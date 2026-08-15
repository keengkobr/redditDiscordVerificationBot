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
VERIFICATION_LOG_CHANNEL_ID = _get_int("VERIFICATION_LOG_CHANNEL_ID", 0)
# Hidden, bot-only channel discord_bot.py posts/reads a Discord Incoming
# Webhook on -- the Devvit app's verdict hand-off (DEVVIT_PIVOT_SPEC.md v4).
# Reddit's HTTP Fetch Policy never approves personal domains, only a fixed
# global allowlist (discord.com is on it) -- so this replaces the
# self-hosted webhook_receiver.py + custom-domain design entirely.
VERIFY_RELAY_CHANNEL_ID = _get_int("VERIFY_RELAY_CHANNEL_ID", 0)

# --- Reddit ---
# No PRAW/OAuth creds here on this branch -- DEVVIT_PIVOT_SPEC.md retires
# reddit_poller.py and the script-app auth path entirely in favor of the
# Devvit app (devvit/) resolving identity/karma itself. That path is parked,
# not deleted -- see main/channelLogging branches if it's ever revived.
SUBREDDIT_NAME = os.getenv("SUBREDDIT_NAME", "")

# Permalink to the pinned "Verify for Discord" post -- discord_bot.py DMs this
# instead of a prefilled-message compose URL.
DEVVIT_POST_URL = os.getenv("DEVVIT_POST_URL", "")

# --- Shared DB ---
DB_PATH = os.getenv("DB_PATH", "verify.db")

# --- Thresholds (PLAN.md Section 4 — tunable) ---
# These no longer drive the actual pass/fail decision (that now happens in the
# Devvit app, via its own settings -- see devvit/devvit.json). They're kept
# here only so the verification-log-channel embed (discord_bot.py) can show
# "needs N+" against each metric. Keep these in sync with the Devvit app's
# minAccountAgeDays/minTotalKarma/minSubredditActivityCount/minSubredditKarma
# settings by hand -- there's no automatic sync between the two right now.
MIN_ACCOUNT_AGE_DAYS = _get_int("MIN_ACCOUNT_AGE_DAYS", 30)
MIN_TOTAL_KARMA = _get_int("MIN_TOTAL_KARMA", 100)
MIN_SUBREDDIT_ACTIVITY_COUNT = _get_int("MIN_SUBREDDIT_ACTIVITY_COUNT", 5)
MIN_SUBREDDIT_KARMA = _get_int("MIN_SUBREDDIT_KARMA", 20)
CODE_EXPIRY_MINUTES = _get_int("CODE_EXPIRY_MINUTES", 30)
CODE_COOLDOWN_SECONDS = _get_int("CODE_COOLDOWN_SECONDS", 60)

# --- Polling ---
POLL_INTERVAL_SECONDS = _get_int("POLL_INTERVAL_SECONDS", 30)


def validate(require_discord: bool = False) -> None:
    """Fail fast with a clear message instead of a confusing library traceback."""
    missing = []
    if require_discord:
        if not DISCORD_BOT_TOKEN:
            missing.append("DISCORD_BOT_TOKEN")
        if not VERIFY_CHANNEL_ID:
            missing.append("VERIFY_CHANNEL_ID")
        if not DEVVIT_POST_URL:
            missing.append("DEVVIT_POST_URL")
        if not VERIFY_RELAY_CHANNEL_ID:
            missing.append("VERIFY_RELAY_CHANNEL_ID")
    if missing:
        raise SystemExit(
            f"Missing required config: {', '.join(missing)}. "
            "Copy .env.example to .env and fill these in."
        )
