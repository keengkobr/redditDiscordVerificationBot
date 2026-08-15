# Feature: Verification Log Channel

> **Implemented.** This was the original design ticket, written against the
> pre-Devvit-pivot architecture (hence the `reddit_poller.py` references
> below) and an earlier embed style (bold-only-on-failure text lines). Both
> have since evolved: the verdict now arrives via the Devvit app + Discord
> webhook relay (see [DEVVIT_PIVOT_SPEC.md](DEVVIT_PIVOT_SPEC.md)), and the
> embed format is a full ✅/❌ requirements checklist shared with the Discord
> DMs, not just bolded failure lines. The schema this ticket introduced is
> current and documented in [PLAN.md](PLAN.md) Section 5. Kept here for the
> original design rationale (why these columns, why `logged_to_discord` is
> tracked independently), not as a description of the current embed format.

## Goal
Post a Discord embed to a dedicated log channel for every completed verification attempt — pass or fail — showing the underlying data (account age, karma, subreddit-specific activity) that produced the verdict.

## New config
Add to `.env.example` and `.env`:
```
VERIFICATION_LOG_CHANNEL_ID=
```

## Schema changes (db.py)
Currently `verifications` stores `status` and `fail_reason` (a string). Widen this so the raw numeric inputs behind every verdict are persisted, not just the final reason — the log embed needs real numbers, not just a category.

Add columns to the `verifications` table:
```sql
ALTER TABLE verifications ADD COLUMN account_age_days INTEGER;
ALTER TABLE verifications ADD COLUMN total_karma INTEGER;
ALTER TABLE verifications ADD COLUMN subreddit_activity_count INTEGER;
ALTER TABLE verifications ADD COLUMN subreddit_karma INTEGER;
ALTER TABLE verifications ADD COLUMN logged_to_discord INTEGER DEFAULT 0;
```

`reddit_poller.py` should write all four values every time it evaluates a user — regardless of pass/fail — not just when the thresholds are met. `logged_to_discord` is a flag so the Discord side can pick up rows it hasn't posted yet without double-posting (see below).

## Where this runs
`discord_bot.py` already needs a loop/task polling the DB for new verdicts (to assign the Verified role and DM the user pass/fail). Add the log-post step into that same loop, right after handling the role/DM logic for each newly-resolved row. Query for rows where `status IN ('verified','failed')` AND `logged_to_discord = 0`, post the embed, then set `logged_to_discord = 1` on that row so it's never posted twice — this also makes the log durable across bot restarts (anything not yet flagged will still get picked up and posted).

## Embed format

**Pass:**
```
✅ Verified — u/{reddit_username} → {discord_mention}
Account age: {account_age_days} days
Total karma: {total_karma}
r/{SUBREDDIT_NAME} activity: {subreddit_activity_count} posts/comments, {subreddit_karma} karma
Verified at: {verified_at, formatted UTC}
```
Color: green (e.g. `0x2ECC71`)

**Fail:**
```
❌ Failed — u/{reddit_username} → {discord_mention}
Account age: {account_age_days} days (needs {MIN_ACCOUNT_AGE_DAYS}+)
Total karma: {total_karma} (needs {MIN_TOTAL_KARMA}+)
r/{SUBREDDIT_NAME} activity: {subreddit_activity_count} posts/comments (needs {MIN_SUBREDDIT_ACTIVITY_COUNT}+), {subreddit_karma} karma (needs {MIN_SUBREDDIT_KARMA}+)
Routed to: #verify-review (or omit this line if no mod-review routing occurred)
```
Color: red (e.g. `0xE74C3C`)

Bold or otherwise highlight whichever specific threshold(s) actually failed, so a moderator scanning the channel can tell at a glance why without doing the math themselves (e.g. if only karma failed, only that line needs emphasis — age/activity passed fine).

## Non-goals for this pass
- No periodic/digest summary report — that's a possible future addition using the same underlying data, not part of this task.
- No filtering/muting of specific failure types — every attempt logs, no exceptions.
- No pagination/history browsing UI — this is a live feed only; historical lookups can query `verify.db` directly if ever needed.

## Testing notes
- Verify a fresh account age/karma/activity fail all render correctly with real numbers, not `None`/`null` in the embed.
- Verify a bot restart mid-run doesn't cause duplicate log posts (the `logged_to_discord` flag should prevent this — confirm by restarting `discord_bot.py` after a verdict has already been logged and confirming it isn't re-posted).
- Confirm the bot's Discord permissions include `Send Messages` and `Embed Links` in the new log channel.
