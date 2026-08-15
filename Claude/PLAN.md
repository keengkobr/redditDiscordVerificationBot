# Discord Verification Bot — Project Plan

> **Update (this branch):** Reddit now requires new integrations to go through Devvit
> rather than registering a classic script-app OAuth client, so the Reddit-side
> pieces described below (PRAW, `reddit_poller.py`, inbox polling) have been
> replaced per [DEVVIT_PIVOT_SPEC.md](DEVVIT_PIVOT_SPEC.md). Sections 4, 5, 9, and
> 11 below are unaffected — same thresholds, same DB schema, same abuse
> prevention, same hidden-profile mitigation, just resolved via the Devvit app
> instead of PRAW. Sections 6, 7 (Phase 1), and 10 are superseded where noted.
> The PRAW path is parked, not deleted — see the `main`/`channelLogging`
> branches if it's ever revived.

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

| Check | Value | Notes |
|---|---|---|
| Account age | 30+ days | Kills same-day burners |
| Total karma | ~~100+~~ 50+ | Weak alone, filters obvious bots |
| Subreddit-specific post/comment count | ~~5+~~ 1+ | Proves real participation, not just account existence |
| Subreddit-specific karma | ~~20+~~ 50+ | Proves contributions weren't universally downvoted/spam |
| Code expiry | 30 min | Prevents stale/shared codes |
| One Reddit account per Discord account | enforced | Stored in DB, rejected on reuse |

Started stricter, loosened based on real testing rather than waiting for false-positive reports — this is exactly the adjustment this section anticipated. Set as Devvit app settings now, not `.env` — see DEVVIT_PIVOT_SPEC.md.

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
| Reddit integration | ~~PRAW~~ Devvit app (TypeScript, "Devvit Web": React + Hono + tRPC) | Reddit requires new integrations to go through Devvit — see DEVVIT_PIVOT_SPEC.md |
| Reddit↔VPS hand-off | ~~Self-hosted webhook (`webhook_receiver.py`, FastAPI/nginx/custom domain)~~ Discord Incoming Webhook, read directly by `discord_bot.py` (`verdict.py`) | Reddit's HTTP Fetch Policy never approves personal/custom domains — only a fixed global allowlist, which includes discord.com, skips review. See DEVVIT_PIVOT_SPEC.md v4. |
| Database | SQLite (sqlite3 / aiosqlite) | Zero-config, plenty for this scale; migrate to Postgres only if the server gets very large |
| Hosting | Single Linux VPS (Hetzner CX22 or DigitalOcean droplet) | ~$5-6/mo, runs just `discord_bot.py` now — no domain/TLS/nginx needed, v3 of this plan's webhook design didn't survive Reddit's domain policy |
| Process management | systemd services (or pm2/supervisord) | Auto-restart on crash/reboot |

---

## 7. Feature Roadmap

### Phase 1 — Core Verification (MVP)
- [x] ~~Reddit API app registration + OAuth credentials~~ Superseded: Devvit app install is immediate, no multi-week approval queue (confirmed) — see DEVVIT_PIVOT_SPEC.md
- [x] Install the Devvit app on the subreddit with **moderator** scope (replaces "make the bot account a moderator" — same 28-day content-visibility rationale, see Section 11)
- [x] `discord_bot.py`: `#verify-here` channel, button, DM flow, role assignment
- [x] ~~`reddit_poller.py`: inbox polling, code matching, threshold checks~~ Retired — replaced by the Devvit app's form-submit handler + a Discord webhook relay (`verdict.py`, `discord_bot.py`)
- [x] Shared SQLite schema + one-account-per-user enforcement
- [x] Plain-language pass/fail DMs
- [x] Mod review channel for manual overrides (also the fallback path for hidden-profile false negatives)
- [x] ~~Domain + TLS (nginx/certbot) in front of `webhook_receiver.py`~~ Built, tested end-to-end, then retired — Reddit's HTTP Fetch Policy never approves personal domains, so this whole approach was a dead end regardless of how well it worked. Replaced by a Discord Incoming Webhook (globally pre-allowed, zero review). See DEVVIT_PIVOT_SPEC.md v4.

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

1. ~~Register the Reddit API app now~~ Superseded, but **correcting an earlier note here** — a third time. The lead-time risk didn't disappear with the Devvit pivot, it moved: first thought resolved (playtest is immediate), then found to have moved to a domain-allowlist review (1-2 business days, confirmed live via a `PERMISSION_DENIED`/"domain ... is not allowed" error) *plus* a separate "creates custom posts" review with no stated turnaround. The domain-review half is now moot entirely — the custom VPS domain that review applied to was replaced with a Discord Incoming Webhook (`discord.com` is globally pre-allowed by Reddit's HTTP Fetch Policy, zero review needed) once it turned out personal domains are *never* approved, not just slow to approve. What remains: only the "creates custom posts" review, gated by `devvit publish`, no stated turnaround, blocking the real-subreddit install. The originally-submitted v0.0.2 was withdrawn and resubmitted as v0.0.8 once the Discord-webhook redesign, loosened thresholds, and UI polish were all in — the earlier submission would have carried the dead custom-domain request regardless of this review's outcome. As of this writing v0.0.8 is submitted, awaiting review. See DEVVIT_PIVOT_SPEC.md v4, Prerequisites #4.
2. Decide final threshold values (Section 4) — can be adjusted post-launch based on false positives. Now set as Devvit app settings, not `.env` — see DEVVIT_PIVOT_SPEC.md.
3. Pick a VPS provider (Hetzner/DigitalOcean/Oracle free tier) and provision it.
4. Decide Phase 4 AI scope (if any) before estimating that cost bucket further.
5. Build the MVP (Phase 1) as the first working prototype.
6. ~~Test the "hidden profile" edge case (Section 11) directly with a mod-account vs non-mod-account before launch.~~ **Confirmed live.** A test account with all posts/comments hidden via Reddit's "Curate your profile" setting made 1 post + 2 comments in the dev subreddit; the moderator-scope Devvit app correctly detected all 3 (`subreddit_activity_count: 3`, `subreddit_karma: 3` — an exact match, not approximate) despite the content being hidden from the public profile. The moderator-scope mitigation works as intended — a legitimate member with a curated profile will not be wrongly soft-failed as `no_visible_activity`.
7. Confirm the mod-gated subreddit-karma call (`user.getUserKarmaFromCurrentSubreddit()`) actually requires moderator status for a user *other than* whoever's testing it — the spike so far only proved it works, not that the mod-gating itself is real (the tester and the mod were the same account). See DEVVIT_PIVOT_SPEC.md "Confirmed via spike" section. Overlaps with item 6 above and can likely be tested together.

---

## 11. Known Risk: Users With Curated/Hidden Profiles

> **Resolved, confirmed live** (see §10 item 6). Kept below for the original
> reasoning — the mitigation described here was tested directly, not assumed,
> and works.

Reddit lets users hide some or all of their posts and comments from their public profile via a "Curate your profile" setting (hide all, or hide per-subreddit). This does **not** delete or hide the comment from the subreddit itself — it only hides it from the user's profile page and, likely, from the same profile-listing API endpoints our verification bot queries (`redditor.comments.new()` / `redditor.submissions.new()`).

**Practical impact:** A genuinely active, legitimate subreddit member could fail the activity check simply because they've curated their profile — a false negative, not a true burner/fresh account.

**Mitigations built into the plan:**
- Make the Devvit app a moderator of the subreddit (moderator-scope `reddit` permission, replacing the original PRAW-era "make the bot account a moderator"). Reddit grants mod teams visibility into a user's full profile content history for 28 days after that user interacts with the community (posts, comments, joins) — **confirmed working**: a test account with 1 post + 2 comments hidden via curate-profile was still fully detected (`subreddit_activity_count: 3`, exact match) by the moderator-scope Devvit app.
- Treat "no visible activity found" as a **soft fail**, not a hard rejection — route it to the `#verify-review` mod channel for a human to check manually (e.g., a mod can browse the subreddit directly or ask the user to temporarily un-hide). Still in place as a fallback for whatever this mitigation doesn't catch (e.g. content hidden longer than the 28-day window, or edge cases not yet hit).
- ~~Before launch, explicitly test this scenario~~ Done — see §10 item 6.
