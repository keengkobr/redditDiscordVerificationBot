# Pivot v4: Devvit Web + Discord Webhook Relay (as-built)

> **Status: implemented on `main`.** This document went through several
> revisions as real testing corrected each prior version's assumptions (v1:
> inbox polling — never confirmed possible; v2: Blocks/forms custom post —
> wrong API generation; v3: Devvit Web + self-hosted VPS webhook — worked
> right up until Reddit's own HTTP Fetch Policy ruled out ever approving a
> personal domain). This version replaces the VPS webhook with a Discord
> Incoming Webhook and is believed final. Earlier sections are kept only
> where the underlying fact is still true (API surface, timings, permission
> gotchas) — the *transport* described in v1-v3 is history, not guidance.

## Why a Discord webhook, not a self-hosted endpoint

v3 built `webhook_receiver.py`: a FastAPI server on the VPS, behind nginx/TLS,
on a real registered domain (`verify.verificationforyou.com`). It worked —
tested end-to-end from the public internet. Then Reddit's own HTTP Fetch
Policy documentation surfaced the actual rule for domain approval:

> **Personal domains** (e.g., personaldomain.com) — **Will not be approved.**

That's categorical, not "pending" or "usually." The domain-allowlist request
for `verify.verificationforyou.com` was never going to clear — it was headed
for denial, not approval, no matter how long it waited. Continuing to build
around a custom VPS domain was a dead end.

The same policy page lists a **global fetch allowlist** — domains any app can
call with zero review, because Reddit has already vetted them. `discord.com`
is on that list. So instead of the Devvit app calling our own server, it POSTs
directly to a **Discord Incoming Webhook**, and `discord_bot.py` — already
holding an open gateway connection to Discord — reads that channel directly
and writes into `verify.db` itself. No HTTP server, no domain, no nginx, no
TLS cert, no review queue for this leg at all.

## What's still true from earlier versions

The Reddit-side identity/karma/threshold logic is unaffected by this change —
only the *transport* for the verdict hand-off changed. Confirmed via a live
spike (`devvit new`, `devvit login`, `devvit playtest`, real form submission,
real API calls) before any of this was built:

1. **The scaffold Devvit ships today is "Devvit Web"**, not Blocks/forms:
   React 19 + Tailwind (client), Hono + tRPC (server), Node v22 serverless.
   Its own `AGENTS.md` warns against `@devvit/public-api`/Blocks code found
   elsewhere. A plain React `<form>` posting to our own tRPC mutation is what
   this app actually uses — not Devvit's native `forms`/`showForm` mechanism.
2. **The real, confirmed API surface** (all from `@devvit/reddit`, re-exported
   via `@devvit/web/server`):
   - `reddit.getCurrentUsername(): Promise<string | undefined>` — resolves
     the submitting user's identity, no separate auth step.
   - `reddit.getUserByUsername(username): Promise<User | undefined>` —
     `undefined` rather than a throw for suspended/deleted/nonexistent accounts.
   - `User.createdAt: Date`, `User.linkKarma: number`, `User.commentKarma: number`.
   - `User.getComments(opts)` / `User.getPosts(opts)` — `{sort, timeframe,
     limit, pageSize}`, `.all()` to materialize. Each result has
     `.subredditName` and `.score` — filtering/summing is done in our code
     (`devvit/src/server/core/verify.ts`), same shape as `reddit_poller.py`'s
     old PRAW loop, different client.
   - `User.getUserKarmaFromCurrentSubreddit()` — purpose-built subreddit-karma
     endpoint, requires moderator status *unless* the target user is the
     querier themself. Implemented and callable, but not wired into
     `evaluate()`'s actual decision (which uses the manually-summed karma from
     `getComments`/`getPosts` instead) — see "Still open" below for why.
   - `context.subredditName`, `context.username` — directly on the shared
     `context` object.
3. **Real timings** (spike, single account): `getCurrentUsername` 125ms,
   `getUserByUsername` 114ms, comments+posts fetch 187ms,
   `getUserKarmaFromCurrentSubreddit` 203ms, **total 629ms** — comfortably
   inside any serverless execution window.
4. **Confirmed live against a real Discord server** (not just the spike):
   the bot's own role had **no channel-specific permission overwrite at all**
   in `#verify-here`, inheriting `@everyone`'s deny on `send_messages`; and
   Discord's newer, separate **Pin Messages** permission (distinct from the
   older "Manage Messages") was also needed for `msg.pin()`. Both are one-time
   Discord-side configuration, not code bugs.

## New user-facing flow

1. User joins Discord → lands in `#verify-here` → clicks **"Verify Reddit
   Account"**.
2. `discord_bot.py` generates a code and DMs the user the pinned **"Verify for
   Discord"** post's permalink plus the code as plain text.
3. User opens the post, pastes the code into the on-page form, hits **Verify**.
4. The `verify.submit` tRPC mutation runs server-side, already knowing who
   submitted it. It computes metrics, evaluates thresholds, and POSTs the
   verdict to a Discord Incoming Webhook.
5. `discord_bot.py`'s `on_message` handler reads that channel, validates the
   message actually came from the webhook it created (`message.webhook_id`),
   parses the JSON verdict out of `message.content`, and calls
   `verdict.process_verdict()` — same DB semantics `webhook_receiver.py` used
   to have (idempotency, expiry, username-conflict handling), just a plain
   function call instead of an HTTP round-trip, since both now run in the
   same process against the same `verify.db`.
6. `discord_bot.py`'s existing poll loop picks up the resolved row — role
   assignment, pass/fail DM, and the verification-log-channel feature —
   unchanged from every prior version.

## Architecture (as-built)

```
+---------------------------------+   HTTPS POST    +----------------------------------+
|  devvit/ (Reddit-hosted,        | --------------->|  discord.com                      |
|  Devvit Web: React+Hono+tRPC)   |  Incoming        |  (Incoming Webhook -- globally    |
|                                  |  Webhook         |  pre-allowed, no Reddit review)   |
|  - Pinned "Verify for Discord"  |                  +----------------------------------+
|    custom post (splash.tsx)     |                                    |
|  - React form: code field       |                                    | posts a message
|  - trpc verify.submit mutation  |                                    v
|    (src/server/trpc.ts):        |                  +----------------------------------+
|    reddit.getCurrentUsername()  |                  |  discord_bot.py (VPS)             |
|    -> computeMetrics() ->       |                  |  - already holds the gateway      |
|    evaluate() -> postVerdict()  |                  |    connection, ensure_relay_webhook|
+---------------------------------+                  |    creates/finds the webhook       |
                                                       |  - on_message(): validate          |
                                                       |    webhook_id, json.loads(content),|
                                                       |    verdict.process_verdict()       |
                                                       |    -> writes verify.db directly    |
                                                       |    (no HTTP hop -- same process)   |
                                                       |  - existing poll loop picks up the |
                                                       |    row: role/DM/log embed          |
                                                       +----------------------------------+
```

Files: `devvit/src/server/core/verify.ts` (metrics + threshold evaluation),
`devvit/src/server/core/webhook.ts` (POST to the Discord webhook),
`devvit/src/server/trpc.ts` (`verify.submit` mutation), `devvit/src/client/splash.tsx`
(the form UI), `devvit/src/server/core/post.ts` + `routes/triggers.ts` (pinned
post on install); `verdict.py` (verdict validation + DB write, shared logic),
`discord_bot.py` (relay webhook creation + `on_message` handler + the
pre-existing role/DM/log-embed poll loop, unchanged).

No scheduler, no cron, no inbox reading, no HTTP server, no domain, no nginx,
no TLS cert. Purely event-driven, entirely within Discord's and Devvit's own
infrastructure plus one VPS process.

## Prerequisites

1. Terms & Conditions and Privacy Policy links, set on Reddit's developer
   settings page — required before `devvit publish` will run at all for any
   app using the `http` plugin, **even one only calling a globally-allowed
   domain**. See `docs/terms.html`/`docs/privacy.html` (GitHub Pages, on `main`).
2. A hidden, bot-only Discord channel (deny `@everyone` View Channel) for
   `discord_bot.py` to create the relay webhook on. `VERIFY_RELAY_CHANNEL_ID`
   in `.env`.
3. The bot needs `Manage Webhooks` in that channel, and `Send Messages` +
   `Pin Messages` + `Embed Links` explicitly granted on its own role in
   `#verify-here` (see "confirmed live" gotchas above — neither is inherited
   by default).
4. **"Creates custom posts" app review — still required, still no stated
   turnaround.** Unaffected by this pivot: `devvit publish` is gated by human
   review for any app that creates custom posts (this one does — the pinned
   post), regardless of what domains it fetches. Submitted, pending, as of
   this writing.
5. ~~Domain name pointed at the VPS IP~~ / ~~TLS certificate~~ / ~~nginx
   reverse proxy~~ / ~~Domain allowlist review~~ — **no longer needed.**
   `discord.com` is globally pre-allowed; there is no domain-specific review
   at all for this design. (v3's now-removed `deploy/nginx-verify.conf.example`
   and the `verify.verificationforyou.com` DNS/cert setup are dead weight —
   safe to tear down, not required for anything in the current design.)

## `verdict.py` (VPS side, called directly by `discord_bot.py`)

Same validation/DB logic `webhook_receiver.py` had, minus the HTTP layer
(no auth header to check — see "Auth model" below for what replaced it):

1. Validate the payload shape (`pydantic`, same fields as before).
2. Look up the pending row by `code`; raise if missing/already-resolved
   (idempotency).
3. Raise if the code has expired since being issued (marks it
   `failed`/`code_expired` first).
4. If `status == "verified"` and the username is already claimed by a
   different *verified* row, raise and record this attempt as
   `failed`/`reddit_account_already_linked` rather than silently dropping it.
5. Write the verdict via `db.set_result()`, leaving `logged_to_discord = 0`
   for the existing poll loop to pick up.

**Auth model, changed from v3:** there's no separate shared secret anymore.
The Discord webhook URL itself is the credential (same as v3's secret header,
just embedded in the URL rather than a separate value) — kept as the Devvit
app's only setting, `webhookUrl`, marked `isSecret: true`. `discord_bot.py`
additionally checks `message.webhook_id` matches the specific webhook it
created, so a message dropped into the relay channel by anything else is
ignored even before the JSON parse.

**Tested** exactly as v3's `webhook_receiver.py` was, adapted for the new
call shape: unknown/expired/double-submitted code rejected, username conflict
rejected (and recorded, not lost), valid verdict written correctly with all
metric columns populated and `logged_to_discord` left at 0 — all against a
real `verify.db`, not mocks. Also ran a full simulated pipeline (Devvit's
exact `{content: JSON.stringify(payload)}` POST body → simulated
`message.content` → `json.loads` → `verdict.process_verdict()`) confirming
the whole chain works at the code level.

**A real, pre-existing bug was found and fixed while building this (see
`db.py`, backported independently to `main`/`channelLogging` too, not
specific to this pivot):** `UNIQUE(reddit_username)` was a table-wide column
constraint, but `is_reddit_username_taken()` only checks `status='verified'`
rows — so writing a second `failed` row for a username verified elsewhere (a
normal retry case) raised `sqlite3.IntegrityError`. Fixed with a partial
unique index scoped to `status='verified'`, with a migration that rebuilds
any pre-existing `verify.db` in place.

## The Devvit app (Reddit side) — `devvit/`

**`devvit.json`**: `permissions.reddit {enable: true, scope: "moderator"}`,
`permissions.http {enable: true, domains: ["discord.com"]}`, a single
`settings.global.webhookUrl` (`isSecret: true` — no separate secret setting
anymore), and the four `min*` threshold settings. `triggers.onAppInstall`
creates the pinned post automatically on install; a moderator-only menu item
recreates it on demand.

**The post:** a single inline custom post (`splash.tsx`) titled "Verify for
Discord" — plain React, code input + Verify button.

**`verify.submit` tRPC mutation** (`devvit/src/server/trpc.ts`):
1. `reddit.getCurrentUsername()` → friendly bail-out if unresolved.
2. `computeMetrics()` (`core/verify.ts`) → account age, total karma,
   subreddit activity count + karma. `null` if `getUserByUsername` can't
   find the account.
3. `getThresholds()` reads the four settings (falling back to PLAN.md §4
   defaults if unset); `evaluate()` applies the same pass/fail + soft-fail
   logic `reddit_poller.py` used to (zero subreddit activity on an
   old-enough account → `no_visible_activity` soft fail, PLAN.md §11).
4. `postVerdict()` (`core/webhook.ts`) POSTs `{content: JSON.stringify(payload)}`
   to the Discord webhook URL. Failure is caught and surfaced as a friendly
   in-app message.

**Section 11 (hidden/curated profile) mitigation: confirmed working, live.**
A test account with 1 post + 2 comments hidden from its public profile via
Reddit's "Curate your profile" setting still had all 3 correctly detected
(`subreddit_activity_count: 3`, `subreddit_karma: 3` — an exact match) by
`computeMetrics()` in the real dev-subreddit run. The moderator-scope
`reddit` permission does what PLAN.md §11 hoped it would — a legitimate
member with a curated profile won't be wrongly soft-failed as
`no_visible_activity`.

## `discord_bot.py` changes

- **New**, replacing `webhook_receiver.py` entirely: `ensure_relay_webhook()`
  (finds or creates the Discord webhook on `VERIFY_RELAY_CHANNEL_ID`, logs
  the URL every time — journalctl already requires sudo, same privilege
  level as `.env`, so there's no real security loss in not hiding it after
  the first run) and an `on_message` handler that validates, parses, and
  calls `verdict.process_verdict()` directly.
- `intents.message_content = True` added — needed to read the relay
  webhook's message content. (A privileged intent; toggle it in the Discord
  Developer Portal same as the existing Members intent.)
- The role/DM/log-embed poll loop logic itself is unchanged — still consumes
  `verifications` rows the same way regardless of how they got populated.
  What *did* change post-launch (below) is presentation, not logic.

## Post-launch polish: embeds everywhere, loosened thresholds

Once the pipeline was confirmed working live, real usage surfaced formatting
and threshold issues worth fixing before calling this done:

- **Thresholds loosened** from the original PLAN.md §4 starting values
  (100 total karma / 5 subreddit activity / 20 subreddit karma) down to
  50 / 1 / 50 — the originals proved unnecessarily strict once tested
  against a real account. Account age (30 days) unchanged. These are Devvit
  app settings (`devvit settings set minTotalKarma` etc.); the VPS `.env`'s
  matching `MIN_*` vars must be updated by hand too (log-embed text only).
- **One consistent requirements-checklist style, shared everywhere**: the
  `#verify-here` pinned message, both pass/fail DMs, the verification-log
  channel embed, and — new — the Devvit post's own result card all render
  the same ✅/❌-per-requirement checklist now, instead of three or four
  different ad-hoc formats. `discord_bot.py`'s `_requirement_lines()` is the
  shared Python-side helper; `verify.submit` now returns `metrics` +
  `thresholds` alongside `message` so `splash.tsx` can render the matching
  checklist client-side.
- **The initial "here's your code" DM became an embed** with the code in a
  fenced code block (one-tap-to-copy on both desktop and mobile, unlike
  backticks buried in a sentence) and a real link-style button
  ("Open Verification Post") instead of a bare pasted URL.
- **Devvit post UI cleanup**: removed a leftover "Docs" footer link
  (scaffold boilerplate pointing at Devvit's own developer docs, irrelevant
  to end users) and fixed a dark-mode bug where the code input's text was
  hardcoded black (`text-black`) while its background followed the system
  theme — invisible on a dark background. Now explicit light/dark colors,
  same pattern as the rest of the page.
- **Known, accepted tradeoff from the Discord-webhook design**: a
  double-submitted code no longer gets accurate real-time feedback on the
  Reddit side. `postVerdict()` only knows whether *Discord* accepted the
  webhook POST, not whether `verdict.process_verdict()` downstream accepted
  or rejected it as a duplicate — so a second submission of an
  already-resolved code shows "Submitted!" same as the first, even though
  nothing changed in the DB and no second DM follows. Minor (only matters on
  a fast double-click), not fixed — v3's design could propagate that
  rejection accurately since it was a direct HTTP round-trip; this is the
  price of posting to Discord's endpoint instead of our own.

## What stayed the same across all versions

- `db.py` schema and all its functions.
- Code generation, expiry, one-account-per-user enforcement.
- Verification-log-channel feature (format evolved post-launch, see above;
  the underlying mechanism — poll loop, `logged_to_discord` flag — didn't).

## What's retired

- `reddit_poller.py`, `praw`/`prawcore`, all `REDDIT_CLIENT_ID`/`SECRET`/
  `USERNAME`/`PASSWORD`/`USER_AGENT` env vars — parked on the `channelLogging`
  branch's history (pre-Devvit-pivot) if ever revived, not present on `main`.
- **`webhook_receiver.py`, `fastapi`/`uvicorn`, `deploy/webhook_receiver.service`,
  `deploy/nginx-verify.conf.example`, `DEVVIT_WEBHOOK_SECRET`, `WEBHOOK_PORT`** —
  the entire v3 self-hosted-endpoint design, replaced by the Discord webhook
  relay. The domain/TLS/nginx setup already done on the VPS is now unused
  dead weight, not a dependency of anything current.

## Explicitly out of scope for this pass

- Migrating `discord_bot.py` to Devvit — not applicable, same reasoning as
  every prior version.
- The classic PRAW script-app path — parked, not abandoned.
- Any per-user dynamic post creation — "Verify for Discord" is a single
  static pinned post.
- Adopting `getUserKarmaFromCurrentSubreddit()` as the subreddit-karma source
  of truth — implemented and confirmed callable, but the self-user-exception
  nuance (does it actually require moderator status for someone *other* than
  the querier, or would our always-self-query usage never actually exercise
  that gate at all?) is still unresolved.

## Testing notes — what's done, what's left

Done:
- Full Reddit-side chain confirmed live against a real account, twice (spike
  + real dev-subreddit run): identity resolution, karma/history pull,
  threshold evaluation.
- `npx tsc --build`, `npm run lint`, `npx vitest run`, `npm run build` all
  pass on `devvit/` after the webhook-relay changes.
- `verdict.py` tested directly (not via HTTP): all the same scenarios v3's
  `webhook_receiver.py` covered, plus a full simulated pipeline matching
  Devvit's exact POST body shape.
- `discord_bot.py` connected to the real Discord server, posted/pinned the
  Verify message (after fixing the two real permission gaps noted above).
- `db.py`'s `IntegrityError` fix, independently verified and backported to
  `main`/`channelLogging`.
- v3's self-hosted webhook (domain + TLS + nginx + `webhook_receiver.py`) was
  fully built and confirmed working end-to-end from the public internet
  before being retired — not abandoned due to a bug, but due to Reddit's
  policy on personal domains making it a dead end regardless of how well it
  worked.
- **The Discord-webhook relay ran live, start to finish, for real.** Clicked
  Verify in Discord → DM → dev-subreddit post → submitted → Devvit's
  `postVerdict()` POSTed to the real Discord webhook → `discord_bot.py`'s
  `on_message` handler received it (`relay: processed verdict for code
  'SSMFBU'`) → the existing poll loop picked up the row and sent the correct
  fail DM (`subreddit_activity:1<5;subreddit_karma:1<20` — an empty dev
  subreddit correctly failing, not a bug) → the verification-log-channel
  embed posted. Two real, live bugs found and fixed along the way, both
  Discord-permission issues rather than logic bugs:
  - Discord rejects webhook names containing the substring "discord"
    (anti-impersonation rule) — `RELAY_WEBHOOK_NAME` renamed.
  - Python block-buffers `print()` under systemd (stdout isn't a tty), so
    our own log lines — including the relay webhook's URL — were silently
    getting lost on process restarts, while `discord.py`'s own
    `logging`-module output kept showing up fine. Fixed with
    `Environment=PYTHONUNBUFFERED=1` in `discord_bot.service`. This means
    some of the earlier "silent failures" during this debugging session may
    have been silently-lost successes, not actual errors — worth remembering
    if something seems to work differently than an old log suggested.
  - (Also needed, same category as the earlier `#verify-here` fixes: the
    verification-log channel needed its own explicit `Send Messages`/`Embed
    Links` overwrite for the bot's role — nothing is inherited by default.)
  - The temporary metrics-logging line added to `trpc.ts` to work around the
    domain block has been removed now that the real path works.
- **Section 11 curated-profile visibility, confirmed** (PLAN.md §10 item 6) —
  see above. A test account's hidden posts/comments were correctly detected
  by the moderator-scope app, exact match on count and karma.

Still open:
- **"Creates custom posts" app review** — no stated turnaround, blocks
  `devvit install` on the real subreddit (`Drueandgabe`), which blocks
  getting the real `DEVVIT_POST_URL`.
- The `getUserKarmaFromCurrentSubreddit()` self-user-exception nuance
  (PLAN.md §10 item 7) — likely moot given it's not wired into `evaluate()`,
  but not formally closed out.
