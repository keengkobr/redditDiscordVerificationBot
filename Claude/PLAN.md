# Discord Verification Bot — Project Plan

## 1. Goal

Gate access to the Discord server behind proof of meaningful, active membership in the subreddit. Block burner/fresh Reddit accounts. Keep the user-facing flow low-friction (buttons + DMs, no slash commands or manual code-pasting into public threads). Leave room to grow the bot into general-purpose community tooling (roles, moderation, chat utilities) without a rewrite.

---

## 2. Architecture

Reddit now requires new integrations to go through **Devvit** (its own app platform) rather than a classic script-app OAuth client — so the Reddit-side identity/karma checks run as a Devvit app, not a Python process polling Reddit's API directly. Full detail and the design history in [DEVVIT_PIVOT_SPEC.md](DEVVIT_PIVOT_SPEC.md).

Two pieces, talking through a Discord Incoming Webhook — no self-hosted HTTP endpoint, no custom
domain, no nginx/TLS, and (as of v5) **no database at all**:

- **`discord_bot.py`** (VPS) — the `#verify-here` channel, the Verify button, the DM flow that collects
  a claimed Reddit username, role assignment, the verification log channel, and the Discord-webhook
  relay that receives verdicts from the Devvit app (`verdict.py`). All in-flight state lives in plain
  in-memory dicts, never on disk — see [DEVVIT_PIVOT_SPEC.md](DEVVIT_PIVOT_SPEC.md) v5 for why (Devvit
  Rules' SOC2/pen-test requirement for services that persist a Reddit-account-to-external-account
  link).
- **`devvit/`** (Reddit-hosted, "Verify for Discord" app) — a pinned post with a form. Unpacks the
  claimed username + Discord user ID carried in the pasted code, resolves the submitting user's real
  Reddit identity for free, checks their account age/karma/subreddit activity, checks/writes its own
  Redis anti-duplicate entry, and POSTs the verdict to a Discord Incoming Webhook that `discord_bot.py`
  reads directly. **Never sends the resolved Reddit username back to Discord** — only a
  `username_ok` boolean; Discord already has the username the user typed into a DM.

**Why a Discord webhook instead of a self-hosted endpoint:** Reddit's HTTP Fetch Policy states personal/custom domains "will not be approved" — only a fixed global allowlist (which includes `discord.com`) skips review. A self-hosted VPS endpoint on a custom domain was fully built and confirmed working end-to-end before this was discovered, then retired — not a bug, just a dead end under that policy.

The classic PRAW/script-app path (`reddit_poller.py`) is parked, not deleted from history — it's on the `channelLogging` branch (pre-Devvit-pivot) if that path is ever revived.

---

## 3. User-Facing Flow

1. User joins Discord → lands in `#verify-here` (only channel visible pre-verification) → sees a pinned embed listing the requirements, with a **Verify Reddit Account** button.
2. Click button → bot DMs asking which Reddit account is theirs.
3. User replies with their Reddit username → bot DMs a one-time code (in a fenced code block, easy to copy — it silently carries the claimed username + Discord ID packed inside it) plus a link button to the subreddit's pinned "Verify for Discord" post.
4. User opens the post, pastes the code into the form, hits Verify.
5. The Devvit app unpacks the claim, resolves the user's real identity, confirms it matches, checks account age/total karma/subreddit-specific activity+karma, and POSTs the verdict (pass/fail + a `username_ok` boolean — never the resolved username itself) to a Discord webhook.
6. `discord_bot.py` reads that webhook and acts immediately (no polling — v5 has no database to poll):
   - **Username didn't match** → re-prompts for the username (up to 3 attempts) before routing to mod review.
   - **Pass** → assigns the Verified role, removes the Unverified role, DMs a green success embed with a requirements checklist.
   - **Fail** → DMs a red (or amber, for the soft-fail case below) embed with the same checklist, marking which requirement(s) didn't meet the threshold, plus a "Request Manual Review" button.
7. Every attempt, pass or fail, also posts an embed to a verification log channel with the same checklist.

No public thread posting, no OAuth server to host, no database anywhere.

---

## 4. Verification Thresholds (tunable)

| Check | Value | Notes |
|---|---|---|
| Account age | 30+ days | Kills same-day burners |
| Total karma | 50+ | Weak alone, filters obvious bots |
| Subreddit-specific post/comment count | 1+ | Proves real participation, not just account existence |
| Subreddit-specific karma | 50+ | Proves contributions weren't universally downvoted/spam |
| Code expiry | 30 min | Prevents stale/shared codes |
| One Reddit account per Discord account | enforced | Devvit's own Redis/KV, TTL'd at 30 days — see Section 5 |

Started stricter (100/5/20 karma/activity/karma), loosened once real testing showed the original numbers were unnecessarily strict. These are **Devvit app settings** (`devvit settings set minTotalKarma` etc.), not `.env` — the VPS's own `MIN_*` env vars only drive the verification-log-channel embed's "needs N+" labels and must be kept matching by hand.

---

## 5. Data Model — no database (v5)

There is no `verify.db`, no SQLite, no persistent store on the VPS at all. Every piece of state that
used to live in a table now lives somewhere that already owned it:

| State | Lives in |
|---|---|
| "This Discord user is verified" | Discord's own role membership |
| "This Reddit account is already linked" (anti-duplicate) | Devvit's own Redis/KV (`reddit_username -> discord_user_id`, 30-day TTL via `redis.set(..., {expiration})`) |
| "Code X is awaiting a verdict for Discord user Y" | `discord_bot.py`'s in-memory `SESSIONS`/`CODE_TO_USER` dicts |
| "Who verified as whom" (audit/mod visibility) | The verification-log channel's own message history |

`SESSIONS` (keyed by `discord_user_id`) holds `{stage, claimed_username, short_id, mismatch_count,
updated_at}` for however long a verification is actually in flight — typically under a minute, capped
at `CODE_EXPIRY_MINUTES` by a periodic sweep. A bot restart drops all of it; that's an accepted
tradeoff (see DEVVIT_PIVOT_SPEC.md v5), not a bug — the user just clicks Verify again.

Full rationale, the claim/match mechanism, and the dedup-write timing details live in
[DEVVIT_PIVOT_SPEC.md](DEVVIT_PIVOT_SPEC.md).

---

## 6. Tech Stack

| Component | Choice | Why |
|---|---|---|
| Discord bot framework | discord.py (2.x) | Mature, supports buttons/views |
| Reddit integration | Devvit app (TypeScript, "Devvit Web": React + Hono + tRPC) | Reddit requires new integrations to go through Devvit |
| Reddit↔Discord hand-off | Discord Incoming Webhook, read directly by `discord_bot.py` (`verdict.py`) | `discord.com` is globally pre-allowed under Reddit's HTTP Fetch Policy; a custom domain is never approved |
| Database | None (v5) | In-flight state is in-memory in `discord_bot.py`; durable state lives in Discord's own roles/channel history and Devvit's own Redis — see Section 5 |
| Hosting | Single Linux VPS (Hetzner CX22 or DigitalOcean droplet) | ~$5-6/mo, runs just `discord_bot.py` — no domain/TLS/nginx/database needed |
| Process management | systemd | Auto-restart on crash/reboot |

---

## 7. Feature Roadmap

### Phase 1 — Core Verification (MVP) — done
- [x] Devvit app installed on the subreddit with moderator scope (needed for subreddit-specific karma and the 28-day hidden-profile visibility carveout — see Section 11)
- [x] `discord_bot.py`: `#verify-here` channel, button, username-claim DM flow, role assignment (including removing the Unverified role on a pass)
- [x] Devvit app's form-submit handler + Discord webhook relay (`verdict.py`) — replaces the original inbox-polling design entirely
- [x] Fully stateless VPS (v5) — no database; in-flight state in memory, durable state in Discord's roles/channel history and Devvit's own Redis (one-account-per-Discord-account enforcement included)
- [x] Self-service `/unlink` command
- [x] Plain-language, embed-based pass/fail DMs with a per-requirement checklist
- [x] Mod review channel for manual overrides (also the fallback path for hidden-profile false negatives and repeated username-mismatch attempts)
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
- Enforce one Reddit account per Discord account via Devvit's own TTL'd Redis entry (see Section 5) — never touches the VPS.
- Store nothing at rest at all (v5) — the only Reddit-linked data that ever exists is in memory on the VPS for the few minutes a verification is in flight, and Devvit never sends the resolved Reddit username back to Discord (only a match/no-match boolean). See DEVVIT_PIVOT_SPEC.md's "Log-scope compliance boundary."
- The Discord webhook relay (Section 2) is the one attack surface: the webhook URL is itself the credential (no separate shared secret), and `discord_bot.py` additionally checks that incoming messages actually came from the specific webhook it created before processing them.
- Mod-only manual override path for legitimate edge cases (private-activity users, recently reactivated accounts, hidden profiles, repeated username-mismatch — see Section 11).

---

## 10. Open Decisions / Next Steps

1. **Reddit's "creates custom posts" app review** — the one remaining blocker before going live on the real subreddit. `devvit publish` is gated by human review for any app that creates custom posts (this one does — the pinned verification post), with no stated turnaround. The v5 rewrite means this needs a fresh submission (the previously-withdrawn v0.0.8 was pre-v5). Once approved: `devvit install <subreddit>` on the real subreddit, grab the real pinned post's URL, swap it into the VPS `.env`'s `DEVVIT_POST_URL`, restart `discord_bot`.
2. **SOC2/persistence question, still unconfirmed in the abstract** — v5's no-database design is the strongest defensible position found without a database at all, but Reddit hasn't explicitly confirmed a fully stateless service falls outside the "external service connecting Reddit user data to external account data" trigger. Being resolved by letting Reddit's app review surface it rather than asking directly (see DEVVIT_PIVOT_SPEC.md's "Remaining open question").
3. **Same-account-retry / dedup-KV-scope validation** — needs empirical confirmation in the dev subreddit before treated as fully settled (see DEVVIT_PIVOT_SPEC.md Section on "Anti-duplicate KV write timing").
4. Pick a VPS provider (Hetzner/DigitalOcean/Oracle free tier) and provision it, if not already done.
5. Decide Phase 4 AI scope (if any) before estimating that cost bucket further.

---

## 11. Known Risk: Users With Curated/Hidden Profiles — resolved, confirmed live

Reddit lets users hide some or all of their posts and comments from their public profile via a "Curate your profile" setting (hide all, or hide per-subreddit). This does **not** delete or hide the comment from the subreddit itself — it only hides it from the user's profile page.

**Practical impact:** A genuinely active, legitimate subreddit member could fail the activity check simply because they've curated their profile — a false negative, not a true burner/fresh account.

**Mitigation, confirmed working:** the Devvit app runs with moderator scope on the subreddit, and Reddit grants mod teams visibility into a user's full profile content history for 28 days after that user interacts with the community. Tested directly: an account with 1 post + 2 comments hidden via curate-profile was still fully detected by the app (`subreddit_activity_count: 3`, `subreddit_karma: 3` — an exact match) despite being hidden from the public profile.

As a backstop for whatever this doesn't catch (content hidden longer than the 28-day window, or edge cases not yet hit), "no visible activity found" is still treated as a **soft fail**, not a hard rejection — routed to the `#verify-review` mod channel for a human to check manually.
