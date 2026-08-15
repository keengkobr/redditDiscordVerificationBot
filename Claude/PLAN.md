# Discord Verification Bot — Project Plan

## 1. Goal

Gate access to the Discord server behind proof of meaningful, active membership in the subreddit. Block burner/fresh Reddit accounts. Keep the user-facing flow low-friction (buttons + DMs, no slash commands or manual code-pasting into public threads). Leave room to grow the bot into general-purpose community tooling (roles, moderation, chat utilities) without a rewrite.

---

## 2. High-Level Architecture

Two lightweight processes run on a single VPS and communicate only through a shared SQLite database:

- **Process 1: `discord_bot.py`** — handles the `#verify-here` channel, the Verify button, DMs, role assignment, and (later) moderation/chat cogs.
- **Process 2: `reddit_poller.py`** — polls the Reddit bot account's inbox for verification codes, checks account age/karma/subreddit activity via PRAW, and writes results to the shared database.
- **Shared: `verify.db` (SQLite)** — stores the reddit_username <-> discord_user_id mapping, pending codes with expiry, and verification history.

No message queue, no API server to host, no OAuth redirect endpoint required — the two processes never talk to each other directly, only through the database.

---

## 3. User-Facing Flow (Recommended: Reddit DM/Chat, Pre-filled)

1. User joins Discord → lands in `#verify-here` (only channel visible pre-verification).
2. Pinned message explains requirements in plain language ("Reddit account 30+ days old, active in r/yoursub") with a **"Verify Reddit Account"** button.
3. Click button → bot DMs the user a pre-filled Reddit message link:
   `https://www.reddit.com/message/compose/?to=YourSubVerify&subject=verify&message=X7K2Q`
4. User taps the link → Reddit opens with the message already filled in → user just hits Send.
5. `reddit_poller.py` picks up the inbox message within ~30–60 seconds, extracts the code, resolves the sender's username.
6. Bot checks: account age, total karma, subreddit-specific comment/post count, subreddit-specific karma.
7. **Pass** → Discord bot assigns the "Verified" role automatically, DMs "You're in!"
8. **Fail** → DM in plain language explaining why, with an option to ping a mod for manual review (routes to a `#verify-review` mod channel).

No public thread posting, no OAuth server to host, one tap plus one send on the user's end.

---

## 4. Verification Thresholds (tunable)

| Check | Starting value | Notes |
|---|---|---|
| Account age | 30+ days | Kills same-day burners |
| Total karma | 100+ | Weak alone, filters obvious bots |
| Subreddit-specific post/comment count | 5+ | Proves real participation, not just account existence |
| Subreddit-specific karma | 20+ | Proves contributions weren't universally downvoted/spam |
| Code expiry | 30 min | Prevents stale/shared codes |
| One Reddit account per Discord account | enforced | Stored in DB, rejected on reuse |

Start stricter, loosen based on false-positive reports from real members — easier to relax later than to clean up a raid after the fact.

---

## 5. Data Model (SQLite)

```sql
CREATE TABLE verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id TEXT NOT NULL,
    reddit_username TEXT,
    status TEXT CHECK(status IN ('pending','verified','failed','revoked')) NOT NULL,
    code TEXT NOT NULL,
    code_created_at INTEGER NOT NULL,
    verified_at INTEGER,
    fail_reason TEXT,
    UNIQUE(reddit_username)
);

CREATE INDEX idx_discord_user ON verifications(discord_user_id);
CREATE INDEX idx_code ON verifications(code);
```

- `reddit_username` has a UNIQUE constraint, which enforces one Reddit account per Discord account.
- `status='pending'` rows older than the expiry window are periodically purged/expired by the poller.
- Optional later addition: a `recheck_at` column for periodic re-verification (see Section 7).

---

## 6. Tech Stack

| Component | Choice | Why |
|---|---|---|
| Discord bot framework | discord.py (2.x) | Mature, supports buttons/views + slash commands if needed later |
| Reddit API wrapper | PRAW | Standard, handles OAuth/rate-limiting for you |
| Database | SQLite (sqlite3 / aiosqlite) | Zero-config, plenty for this scale; migrate to Postgres only if the server gets very large |
| Hosting | Single Linux VPS (Hetzner CX22 or DigitalOcean droplet) | ~$5-6/mo, runs both processes comfortably |
| Process management | systemd services (or pm2/supervisord) | Auto-restart on crash/reboot |

---

## 7. Feature Roadmap

### Phase 1 — Core Verification (MVP)
- [ ] Reddit API app registration + OAuth credentials (2-4 week manual approval — start this immediately)
- [ ] Make the Reddit bot account a **moderator** of the subreddit (needed for the 28-day content-visibility carveout — see Section 11)
- [ ] `discord_bot.py`: `#verify-here` channel, button, DM flow, role assignment
- [ ] `reddit_poller.py`: inbox polling, code matching, threshold checks
- [ ] Shared SQLite schema + one-account-per-user enforcement
- [ ] Plain-language pass/fail DMs
- [ ] Mod review channel for manual overrides (also the fallback path for hidden-profile false negatives)

### Phase 2 — Role & Server Management
- [ ] Additional self-serve roles (interest/topic roles via reaction or button menus)
- [ ] Welcome message automation
- [ ] Periodic re-check job (monthly cron): re-run activity check on verified users, auto-revoke role if Reddit account deleted/suspended or activity drops off

### Phase 3 — Moderation & Chat Utilities
- [ ] Basic automod (word filters, spam/link detection, raid protection)
- [ ] Logging (joins/leaves, role changes, deleted messages) to a mod-log channel
- [ ] Utility commands (server info, user info, simple polls)

### Phase 4 — Optional AI-Backed Features (cost-bearing, scope separately)
- [ ] LLM-backed chat assistant / auto-summarization
- [ ] Smart moderation (tone/context-aware flagging)
- Budget separately — this is the one place real ongoing dollar cost shows up (roughly $5-20+/mo depending on usage). Everything else above is effectively free beyond VPS hosting.

---

## 8. Cost Summary

| Item | Cost |
|---|---|
| Reddit API (free tier, non-commercial, ~100 QPM) | $0/mo |
| Discord Bot API | $0/mo (no usage-based charges) |
| VPS (1 vCPU / 1-2GB RAM, both processes) | ~$5-6/mo (or $0 on Oracle Cloud free tier if available) |
| Optional AI chat features (Phase 4 only) | ~$5-20+/mo, usage-dependent |
| MVP total (Phases 1-3) | ~$0-6/month |

---

## 9. Security & Abuse Prevention

- Rate-limit code generation per Discord user to prevent spam.
- Expire unused codes after 30 minutes.
- Enforce one Reddit account per Discord account via DB constraint.
- Store only what's needed (Reddit username, Discord user ID, verification metadata) — no passwords, no OAuth tokens with this method.
- Mod-only manual override path for legitimate edge cases (private-activity users, recently reactivated accounts, hidden profiles — see Section 11).

---

## 10. Open Decisions / Next Steps

1. Register the Reddit API app now — it's the longest lead-time item (2-4 week approval).
2. Decide final threshold values (Section 4) — can be adjusted post-launch based on false positives.
3. Pick a VPS provider (Hetzner/DigitalOcean/Oracle free tier) and provision it.
4. Decide Phase 4 AI scope (if any) before estimating that cost bucket further.
5. Build the MVP (Phase 1) as the first working prototype.
6. Test the "hidden profile" edge case (Section 11) directly with a mod-account vs non-mod-account before launch.

---

## 11. Known Risk: Users With Curated/Hidden Profiles

Reddit lets users hide some or all of their posts and comments from their public profile via a "Curate your profile" setting (hide all, or hide per-subreddit). This does **not** delete or hide the comment from the subreddit itself — it only hides it from the user's profile page and, likely, from the same profile-listing API endpoints our verification bot queries (`redditor.comments.new()` / `redditor.submissions.new()`).

**Practical impact:** A genuinely active, legitimate subreddit member could fail the activity check simply because they've curated their profile — a false negative, not a true burner/fresh account.

**Mitigations built into the plan:**
- Make the Reddit bot account a moderator of the subreddit. Reddit grants mod teams visibility into a user's full profile content history for 28 days after that user interacts with the community (posts, comments, joins) — this may restore visibility for otherwise-hidden activity, though this should be verified directly rather than assumed.
- Treat "no visible activity found" as a **soft fail**, not a hard rejection — route it to the `#verify-review` mod channel for a human to check manually (e.g., a mod can browse the subreddit directly or ask the user to temporarily un-hide).
- Before launch, explicitly test this scenario: have a test account hide its comment history and confirm what the poller sees with and without mod-account status.
