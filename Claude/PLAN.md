# Discord Verification Bot — Project Plan

## 1. Goal

Gate access to the Discord server behind proof of meaningful, active membership in the subreddit. Block burner/fresh Reddit accounts. Keep the user-facing flow low-friction (buttons + DMs, no slash commands or manual code-pasting into public threads). Leave room to grow the bot into general-purpose community tooling (roles, moderation, chat utilities) without a rewrite.

---

## 2. Architecture

Reddit now requires new integrations to go through **Devvit** (its own app platform) rather than a classic script-app OAuth client — so the Reddit-side identity/karma checks run as a Devvit app, not a Python process polling Reddit's API directly. Full detail and the design history in [DEVVIT_PIVOT_SPEC.md](DEVVIT_PIVOT_SPEC.md).

Two pieces, talking through a shared SQLite file and a Discord Incoming Webhook — no self-hosted HTTP endpoint, no custom domain, no nginx/TLS anywhere in this design:

- **`discord_bot.py`** (VPS) — the `#verify-here` channel, the Verify button, DMs, role assignment, the verification log channel, and the Discord-webhook relay that receives verdicts from the Devvit app (`verdict.py`).
- **`devvit/`** (Reddit-hosted, "Verify for Discord" app) — a pinned post with a form. Resolves the submitting user's Reddit identity for free, checks their account age/karma/subreddit activity, and POSTs the verdict to a Discord Incoming Webhook that `discord_bot.py` reads directly.
- **Shared: `verify.db`** (SQLite) — the `discord_bot.py`/`verdict.py` side of the hand-off; the Devvit app never touches it directly.

**Why a Discord webhook instead of a self-hosted endpoint:** Reddit's HTTP Fetch Policy states personal/custom domains "will not be approved" — only a fixed global allowlist (which includes `discord.com`) skips review. A self-hosted VPS endpoint on a custom domain was fully built and confirmed working end-to-end before this was discovered, then retired — not a bug, just a dead end under that policy.

The classic PRAW/script-app path (`reddit_poller.py`) is parked, not deleted from history — it's on the `channelLogging` branch (pre-Devvit-pivot) if that path is ever revived.

---

## 3. User-Facing Flow

1. User joins Discord → lands in `#verify-here` (only channel visible pre-verification) → sees a pinned embed listing the requirements, with a **Verify Reddit Account** button.
2. Click button → bot DMs the user a one-time code (in a fenced code block, easy to copy) plus a link button to the subreddit's pinned "Verify for Discord" post.
3. User opens the post, pastes the code into the form, hits Verify.
4. The Devvit app resolves the user's identity, checks account age/total karma/subreddit-specific activity+karma, and POSTs the verdict to a Discord webhook.
5. `discord_bot.py` reads that webhook, writes the result to `verify.db`, then on its next poll pass:
   - **Pass** → assigns the Verified role, DMs a green success embed with a requirements checklist.
   - **Fail** → DMs a red (or amber, for the soft-fail case below) embed with the same checklist, marking which requirement(s) didn't meet the threshold, plus a "Request Manual Review" button.
6. Every attempt, pass or fail, also posts an embed to a verification log channel with the same checklist.

No public thread posting, no manual code-pasting, no OAuth server to host.

---

## 4. Verification Thresholds (tunable)

| Check | Value | Notes |
|---|---|---|
| Account age | 30+ days | Kills same-day burners |
| Total karma | 50+ | Weak alone, filters obvious bots |
| Subreddit-specific post/comment count | 1+ | Proves real participation, not just account existence |
| Subreddit-specific karma | 50+ | Proves contributions weren't universally downvoted/spam |
| Code expiry | 30 min | Prevents stale/shared codes |
| One Reddit account per Discord account | enforced | Stored in DB, rejected on reuse |

Started stricter (100/5/20 karma/activity/karma), loosened once real testing showed the original numbers were unnecessarily strict. These are **Devvit app settings** (`devvit settings set minTotalKarma` etc.), not `.env` — the VPS's own `MIN_*` env vars only drive the verification-log-channel embed's "needs N+" labels and must be kept matching by hand.

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
    processed_at INTEGER,
    account_age_days INTEGER,
    total_karma INTEGER,
    subreddit_activity_count INTEGER,
    subreddit_karma INTEGER,
    logged_to_discord INTEGER DEFAULT 0
);

CREATE UNIQUE INDEX idx_unique_verified_reddit_username
    ON verifications(reddit_username) WHERE status = 'verified';
CREATE INDEX idx_discord_user ON verifications(discord_user_id);
CREATE INDEX idx_code ON verifications(code);
CREATE INDEX idx_status_processed ON verifications(status, processed_at);
CREATE INDEX idx_status_logged ON verifications(status, logged_to_discord);
```

- The unique constraint on `reddit_username` is a **partial index scoped to `status='verified'`**, not a table-wide constraint — it enforces one Reddit account per Discord account among active/verified claims, without blocking a legitimate retry (a second `failed` row for a username that's verified elsewhere, or two `failed` attempts sharing a username) from being recorded.
- `account_age_days`/`total_karma`/`subreddit_activity_count`/`subreddit_karma` hold the raw numbers behind every verdict, pass or fail — what the log channel and DMs render as the requirements checklist.
- `processed_at` and `logged_to_discord` are tracked independently, so a crash between role/DM handling and log-posting doesn't lose either step — each is retried on its own next poll regardless of whether the other already succeeded.
- `status='pending'` rows older than the expiry window are periodically expired.
- `db.py`'s `init_db()`/`_migrate()` upgrade an older `verify.db` (including one still on the old table-wide `UNIQUE` constraint) in place — no manual migration step.

---

## 6. Tech Stack

| Component | Choice | Why |
|---|---|---|
| Discord bot framework | discord.py (2.x) | Mature, supports buttons/views |
| Reddit integration | Devvit app (TypeScript, "Devvit Web": React + Hono + tRPC) | Reddit requires new integrations to go through Devvit |
| Reddit↔Discord hand-off | Discord Incoming Webhook, read directly by `discord_bot.py` (`verdict.py`) | `discord.com` is globally pre-allowed under Reddit's HTTP Fetch Policy; a custom domain is never approved |
| Database | SQLite (sqlite3) | Zero-config, plenty for this scale |
| Hosting | Single Linux VPS (Hetzner CX22 or DigitalOcean droplet) | ~$5-6/mo, runs just `discord_bot.py` — no domain/TLS/nginx needed |
| Process management | systemd | Auto-restart on crash/reboot |

---

## 7. Feature Roadmap

### Phase 1 — Core Verification (MVP) — done
- [x] Devvit app installed on the subreddit with moderator scope (needed for subreddit-specific karma and the 28-day hidden-profile visibility carveout — see Section 11)
- [x] `discord_bot.py`: `#verify-here` channel, button, DM flow, role assignment
- [x] Devvit app's form-submit handler + Discord webhook relay (`verdict.py`) — replaces the original inbox-polling design entirely
- [x] Shared SQLite schema + one-account-per-user enforcement
- [x] Plain-language, embed-based pass/fail DMs with a per-requirement checklist
- [x] Mod review channel for manual overrides (also the fallback path for hidden-profile false negatives)
- [x] Verification log channel — an embed for every completed attempt, pass or fail

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
| Reddit (Devvit app, free tier) | $0/mo |
| Discord Bot API | $0/mo (no usage-based charges) |
| VPS (1 vCPU / 1-2GB RAM) | ~$5-6/mo (or $0 on Oracle Cloud free tier if available) |
| Optional AI chat features (Phase 4 only) | ~$5-20+/mo, usage-dependent |
| MVP total (Phases 1-3) | ~$0-6/month |

---

## 9. Security & Abuse Prevention

- Rate-limit code generation per Discord user to prevent spam.
- Expire unused codes after 30 minutes.
- Enforce one Reddit account per Discord account via a partial-unique DB constraint (scoped to verified rows — see Section 5).
- Store only what's needed (Reddit username, Discord user ID, verification metadata) — no passwords, no OAuth tokens.
- The Discord webhook relay (Section 2) is the one attack surface: the webhook URL is itself the credential (no separate shared secret), and `discord_bot.py` additionally checks that incoming messages actually came from the specific webhook it created before processing them.
- Mod-only manual override path for legitimate edge cases (private-activity users, recently reactivated accounts, hidden profiles — see Section 11).

---

## 10. Open Decisions / Next Steps

1. **Reddit's "creates custom posts" app review** — the one remaining blocker. `devvit publish` is gated by human review for any app that creates custom posts (this one does — the pinned verification post), with no stated turnaround. Submitted (current version: see `devvit/devvit.json`), awaiting approval. Once approved: `devvit install <subreddit>` on the real subreddit, grab the real pinned post's URL, swap it into the VPS `.env`'s `DEVVIT_POST_URL`, restart `discord_bot`.
2. Pick a VPS provider (Hetzner/DigitalOcean/Oracle free tier) and provision it, if not already done.
3. Decide Phase 4 AI scope (if any) before estimating that cost bucket further.

---

## 11. Known Risk: Users With Curated/Hidden Profiles — resolved, confirmed live

Reddit lets users hide some or all of their posts and comments from their public profile via a "Curate your profile" setting (hide all, or hide per-subreddit). This does **not** delete or hide the comment from the subreddit itself — it only hides it from the user's profile page.

**Practical impact:** A genuinely active, legitimate subreddit member could fail the activity check simply because they've curated their profile — a false negative, not a true burner/fresh account.

**Mitigation, confirmed working:** the Devvit app runs with moderator scope on the subreddit, and Reddit grants mod teams visibility into a user's full profile content history for 28 days after that user interacts with the community. Tested directly: an account with 1 post + 2 comments hidden via curate-profile was still fully detected by the app (`subreddit_activity_count: 3`, `subreddit_karma: 3` — an exact match) despite being hidden from the public profile.

As a backstop for whatever this doesn't catch (content hidden longer than the 28-day window, or edge cases not yet hit), "no visible activity found" is still treated as a **soft fail**, not a hard rejection — routed to the `#verify-review` mod channel for a human to check manually.
