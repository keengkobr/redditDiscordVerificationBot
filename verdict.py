"""Shared verdict-payload validation (DEVVIT_PIVOT_SPEC.md v5).

v4 had this module write the verdict into verify.db. v5 has no database at
all -- discord_bot.py holds all in-flight state in memory (see SESSIONS/
CODE_TO_USER in discord_bot.py) -- so this module's only remaining job is
validating the shape of what the Devvit app posts to the relay webhook
before discord_bot.py acts on it.

Deliberately has no reddit_username field: Devvit never sends the resolved
Reddit username back to Discord at all, only a username_ok boolean. The
claimed username discord_bot.py displays in DMs/logs is Discord-side data
the user typed into a DM themselves -- it never needs to round-trip through
Devvit to be trusted.
"""

from typing import Optional

from pydantic import BaseModel, Field, ValidationError


class VerdictPayload(BaseModel):
    code: str  # the short id half of the compound code -- see discord_bot.py
    status: str = Field(pattern="^(verified|failed)$")
    username_ok: bool
    fail_reason: Optional[str] = None
    account_age_days: Optional[int] = None
    total_karma: Optional[int] = None
    subreddit_activity_count: Optional[int] = None
    subreddit_karma: Optional[int] = None


class VerdictError(Exception):
    """Raised for a malformed payload -- callers decide how to log it."""


def parse_verdict(raw: dict) -> VerdictPayload:
    try:
        return VerdictPayload(**raw)
    except ValidationError as exc:
        raise VerdictError(f"invalid payload: {exc}") from exc
