# Pivot v3: Devvit Web Verification Post (as-built)

> **Status: implemented on this branch (`devvit-pivot`).** This document started as a
> forward-looking spec (v1: inbox polling, v2: Blocks/forms custom post) and is now
> updated to describe what was actually built and confirmed, after a live spike
> against the real Devvit CLI/runtime surfaced two things v2 got wrong. See "What the
> spike changed" below before reading anything else in this doc as ground truth.

## Why this design over the DM/inbox version

The original Devvit pivot (v1) relied on a scheduled job polling the bot account's
Reddit inbox for DM'd codes — mirroring what `reddit_poller.py` did with PRAW. That
requires Devvit's `reddit` permission to expose inbox/message-reading methods, which
was never confirmed to exist.

v2 avoided that by moving to a **custom interactive post with a form**, since the
app gets the submitting user's authenticated Reddit identity for free via `context`
the moment they interact with it. That core idea held up. What didn't hold up was
*how* v2 assumed that post/form would be built.

## What the spike changed

A live spike (`devvit new`, `devvit login`, `devvit playtest` against a throwaway
dev subreddit, real form submission, real API calls, real timings) found:

1. **The scaffold Devvit ships today is not Blocks/forms.** `devvit new` (CLI
   0.14.0) generates a **"Devvit Web"** app: React 19 + Tailwind on the client,
   Hono + tRPC on the server, running in a Node v22 serverless environment. Its own
   `AGENTS.md` explicitly warns against using `@devvit/public-api`/Blocks code found
   elsewhere — that's the legacy API this project is *not* using. The
   `forms` key in `devvit.json` still exists in the current schema (it maps form
   identifiers to internal endpoint URLs), but nothing in this implementation uses
   it — a plain React `<form>` posting to our own tRPC mutation is simpler, is the
   pattern the current scaffold is built around, and was proven working end-to-end
   in the spike. v2's `forms`/`acceptLabel`/`showToast` JSON snippets below are
   **not what got built** — kept struck through for history, not as guidance.
2. **The real, confirmed API surface** (all from `@devvit/reddit`, re-exported via
   `@devvit/web/server`) is:
   - `reddit.getCurrentUsername(): Promise<string | undefined>` — resolves the
     submitting user's identity. No separate auth step.
   - `reddit.getUserByUsername(username): Promise<User | undefined>` — `undefined`
     rather than a throw for suspended/deleted/nonexistent accounts.
   - `User.createdAt: Date`, `User.linkKarma: number`, `User.commentKarma: number`.
   - `User.getComments(opts): Listing<Comment>` / `User.getPosts(opts): Listing<Post>`
     — `{ sort, timeframe, limit, pageSize }`, `.all()` to materialize. Each
     `Comment`/`Post` has `.subredditName` and `.score` — filtering to the target
     subreddit and summing score is done client-side in our code, same shape as
     `reddit_poller.py`'s PRAW loop, just via a different client.
   - `User.getUserKarmaFromCurrentSubreddit(): Promise<{fromComments?, fromPosts?}>`
     — a purpose-built endpoint for subreddit-specific karma. Docs state it requires
     the requester to be a moderator of the subreddit *unless* the target user is the
     querier themself. **Implemented but not fully load-bearing**: the manually-summed
     `subredditKarma` (from `getComments`/`getPosts` above) is what `evaluate()`
     actually uses; this call isn't currently in `devvit/src/server/core/verify.ts` —
     if adopted later it would need the self-user-exception caveat below resolved
     first.
   - `context.subredditName: string`, `context.username: string | undefined` — both
     directly on the shared `context` object, no extra call needed.
3. **Real timings** (spike, single account, `r/spike_test_app_dev`):
   `getCurrentUsername` 125ms, `getUserByUsername` 114ms, comments+posts fetch
   187ms, `getUserKarmaFromCurrentSubreddit` 203ms, **total 629ms**. Comfortably
   inside any reasonable serverless execution window — the timeout risk flagged
   before the spike is resolved.
4. **One nuance still open, not yet closed out:** the spike's tester and the
   subreddit's moderator were the same account, so `getUserKarmaFromCurrentSubreddit`
   succeeding there doesn't yet distinguish "worked because of genuine moderator
   access" from "worked because it was the querier's own karma" (the documented
   exception path). PLAN.md §10 item 7 tracks closing this with a second account.

## New user-facing flow (unchanged from v2's intent)

1. User joins Discord → lands in `#verify-here` → clicks **"Verify Reddit Account"**.
2. `discord_bot.py` generates a code and DMs the user the pinned **"Verify for
   Discord"** post's permalink plus the code as plain text (see `discord_bot.py`'s
   `handle_verify_click`).
3. User opens the post, pastes the code into the on-page form, hits **Verify**.
4. The form-submit handler (a tRPC mutation, not a native Devvit form) runs
   server-side, already knowing who submitted it via `reddit.getCurrentUsername()`.
   It computes metrics, evaluates thresholds, and POSTs the verdict to the VPS.
5. `webhook_receiver.py` looks up the pending row by code (`db.get_pending_by_code`),
   writes the verdict, exactly as `reddit_poller.py` used to.
6. `discord_bot.py`'s existing poll loop picks up the resolved row — role
   assignment, pass/fail DM, and the verification-log-channel feature — unchanged.

## Architecture (as-built)

```
+---------------------------------+   HTTPS POST    +--------------------------------+
|  devvit/ (Reddit-hosted,        | --------------->|  VPS: webhook_receiver.py       |
|  Devvit Web: React+Hono+tRPC)   |  /devvit/verdict |  (FastAPI + uvicorn)            |
|                                  |  + X-Devvit-Secret|                                |
|  - Pinned "Verify for Discord"  |  header          |  db.get_pending_by_code(),      |
|    custom post (splash.tsx)     |                  |  db.is_reddit_username_taken(), |
|  - React form: code field       |                  |  db.set_result() -- all reused  |
|  - trpc verify.submit mutation  |                  |  from db.py, not duplicated     |
|    (src/server/trpc.ts):        |                  |                                  |
|    reddit.getCurrentUsername()  |                  |  writes verdict into verify.db, |
|    -> computeMetrics() ->       |                  |  leaves logged_to_discord=0     |
|    evaluate() -> postVerdict()  |                  +--------------------------------+
+---------------------------------+                                |
                                                                     v
                                                       discord_bot.py picks up the
                                                       resolved row on its next poll
                                                       pass -- unchanged.
```

Files: `devvit/src/server/core/verify.ts` (metrics + threshold evaluation, mirrors
`reddit_poller.py`'s `check_thresholds()`), `devvit/src/server/core/webhook.ts`
(the POST call), `devvit/src/server/trpc.ts` (`verify.submit` mutation),
`devvit/src/client/splash.tsx` (the form UI), `devvit/src/server/core/post.ts` +
`devvit/src/server/routes/triggers.ts` (creates the pinned post on app install).

No scheduler, no cron, no inbox reading. Purely event-driven.

## Prerequisites

1. Domain name pointed at the VPS IP.
2. TLS certificate (Let's Encrypt/certbot) — Devvit's `http.domains` allowlist
   requires HTTPS. See `deploy/nginx-verify.conf.example` for the exact steps.
3. nginx (or similar) reverse proxy on the VPS terminating TLS, forwarding to
   `webhook_receiver.py` on a local port (`127.0.0.1:8000` by default).
4. Terms & Conditions and Privacy Policy links, set on Reddit's developer
   settings page — required before `devvit publish` will run at all for any
   app using the `http` plugin. See `docs/terms.html`/`docs/privacy.html`
   (GitHub Pages, on `main`) for what was actually used.
5. **Real Reddit App Review — confirmed required, turnaround unknown.**
   Correcting an earlier note in this doc: `devvit playtest`/`devvit upload`
   against a throwaway dev subreddit (<200 subscribers) *is* immediate, no
   review queue for *installing the app itself* — that part of the earlier
   "confirmed moot" note was accurate. But installing on a real subreddit
   requires `devvit publish`, and publish is gated by human review for any
   app that "creates custom posts" (this one does — the pinned "Verify for
   Discord" post). `devvit publish` submits the version and returns
   immediately with "You'll receive an email when your app has been
   approved" — no turnaround estimate given. This reintroduces exactly the
   kind of unknown-lead-time item PLAN.md §10 originally flagged for the PRAW
   script-app registration (2-4 weeks) — it didn't go away with the Devvit
   pivot, it just moved to a different gate later in the process. Until
   approval lands, `devvit install <subreddit>` cannot be run and the pinned
   post cannot be created on the real subreddit, which blocks getting
   `DEVVIT_POST_URL` and therefore blocks starting `discord_bot.py` for real
   (`config.validate()` requires it).
6. **Correction, confirmed live: the `http` domain permission is its OWN,
   separate review gate — distinct from the "creates custom posts" review in
   item 5, and with an actual documented turnaround.** Per Reddit's own HTTP
   Fetch docs: "Requested domains will be submitted for review when you
   playtest or upload your app. Most domain requests are reviewed within
   1–2 business days, though requests with policy ambiguity may take
   longer." This was not expected — playtest/upload being review-free was
   assumed to cover the whole app, including its `permissions.http.domains`
   grant. It doesn't; domains get their own queue. Confirmed live: calling
   `fetch()` to `verify.verificationforyou.com` from a playtest install fails
   every time with
   ```
   Error: 7 PERMISSION_DENIED: grpc invocation failed with status 7;
   HTTP request to domain: verify.verificationforyou.com is not allowed
   ```
   — reproduced identically after an explicit fresh `devvit upload` +
   `devvit playtest` reinstall, ruling out staleness. Approved domains show up
   at `https://developers.reddit.com/apps/verify-for-discord/developer-settings`
   — check there for status rather than re-testing repeatedly. Also per the
   docs: domain entries must be an exact hostname (no wildcards, no protocol,
   no path) — `verify.verificationforyou.com` as configured is already
   correctly formatted, so nothing to fix there.

   Net effect on the "test the full pipeline in a dev subreddit while waiting
   for review" plan (PLAN.md, this doc's Testing notes): it only works up
   through the `fetch()` call until the *domain* review clears (likely
   1-2 business days — separate from and probably faster than item 5's
   custom-post review, which has no stated estimate). Identity resolution,
   karma/history pulls, and threshold evaluation all run for real today; the
   webhook leg specifically needs the domain approval first.

## `webhook_receiver.py` (VPS side) — implemented

**Endpoint:** `POST /devvit/verdict`

**Auth:** shared-secret header (`X-Devvit-Secret`, checked via `hmac.compare_digest`
against `DEVVIT_WEBHOOK_SECRET` in `.env`) — constant-time comparison, this is the
one public-facing attack surface this project has.

**Request body (JSON)** — matches `devvit/src/server/core/webhook.ts`'s `VerdictPayload`:
```json
{
  "code": "X7K2Q9",
  "reddit_username": "some_redditor",
  "status": "verified",
  "fail_reason": null,
  "account_age_days": 412,
  "total_karma": 3204,
  "subreddit_activity_count": 12,
  "subreddit_karma": 87
}
```

**Logic (implemented, test-covered — see below):**
1. Validate shared secret → 401 if missing/wrong.
2. Look up pending row by `code`; 400 if missing/already-resolved (idempotency).
3. 400 if the code has expired since being issued.
4. If `status == "verified"` and the username is already claimed by a different
   *verified* row → 409, and record this attempt as `failed` /
   `reddit_account_already_linked` rather than silently dropping it.
5. Write the verdict via `db.set_result()`, leaving `logged_to_discord = 0` for
   `discord_bot.py` to pick up. Return 200.

**A real bug was found and fixed while testing this path:** `db.py`'s
`UNIQUE(reddit_username)` was a table-wide column constraint, but
`is_reddit_username_taken()` only checks `status='verified'` rows — so writing a
*second* `failed` row for a username that's verified elsewhere (a completely normal
case: someone fails, then retries with the same Reddit account) raised
`sqlite3.IntegrityError` and crashed the write. Fixed by replacing the column
constraint with a **partial unique index** scoped to `status='verified'**:
```sql
CREATE UNIQUE INDEX idx_unique_verified_reddit_username
ON verifications(reddit_username) WHERE status = 'verified';
```
`db.py`'s `_migrate()` rebuilds the table in place for any `verify.db` created
under the old schema (SQLite can't `ALTER TABLE DROP CONSTRAINT`). This bug
predates this pivot — it affects `main`/`channelLogging` too and should be
backported there.

Verified with a real `TestClient` run against `webhook_receiver.py`: missing/wrong
secret → 401, unknown/expired/double-submitted code → 400, username conflict → 409
(and the conflicting attempt is recorded, not lost), valid verdict → 200 with all
metric columns populated and `logged_to_discord` left at 0.

## The Devvit app (Reddit side) — implemented, `devvit/`

Built via `devvit new` (CLI 0.14.0) using the **default "Devvit Web" template**,
not a hand-rolled Blocks/forms app.

**`devvit.json`** (see the real file for the full version): `permissions.reddit
{enable: true, scope: "moderator"}`, `permissions.http {enable: true, domains:
[...]}`, a `settings.global` block for `webhookUrl`, `webhookSecret` (`isSecret:
true`), and the four `min*` thresholds — set via `devvit settings set <key>`, not a
dotenv file. `triggers.onAppInstall` creates the pinned post automatically on
install; a moderator-only menu item recreates it on demand.

**The post:** a single inline custom post (`splash.tsx`) titled "Verify for
Discord" — plain React, not a native Devvit form. Shows a text input for the code
and a Verify button.

**`verify.submit` tRPC mutation** (`devvit/src/server/trpc.ts`), replacing all of
`reddit_poller.py`'s logic:
1. `reddit.getCurrentUsername()` → bail with a friendly message if unresolved.
2. `computeMetrics()` (`core/verify.ts`) → account age, total karma, subreddit
   activity count + karma, via the real API calls confirmed above. `null` if
   `getUserByUsername` can't find the account.
3. `getThresholds()` reads the four settings (falling back to PLAN.md §4 defaults
   if unset) and `evaluate()` applies the same pass/fail + soft-fail logic as
   `reddit_poller.py`'s `check_thresholds()` (zero subreddit activity on an
   old-enough account → `no_visible_activity` soft fail, PLAN.md §11).
4. `postVerdict()` (`core/webhook.ts`) POSTs to `webhookUrl` with the
   `webhookSecret` header. Failure here is caught and surfaced as a friendly
   in-app message rather than a raw error.

**Section 11 (hidden/curated profile) mitigation:** moderator-scope `reddit`
permission is requested, matching the plan's rationale. Still needs the same
verification PLAN.md always called for (test with a mod-account vs a non-mod
Devvit install) — the spike didn't distinguish this because the tester was the
subreddit's owner/mod. See PLAN.md §10 items 6–7.

## `discord_bot.py` changes (implemented)

Only the DM copy in `handle_verify_click` changed — no changes to role assignment,
pass/fail DM logic, or the verification-log-channel feature, all of which consume
`verifications` rows regardless of how they were populated:
- **Before:** DM contained a prefilled Reddit message-compose link built from
  `REDDIT_USERNAME`.
- **After:** DM contains the code as plain text plus `config.DEVVIT_POST_URL` (a
  new required env var — `config.validate(require_discord=True)` now checks for
  it), with instructions to open the post and paste the code in.

## What stayed the same

- `db.py` schema and all its functions — same functions, same signatures, reused
  as-is by `webhook_receiver.py` (not duplicated). (The `UNIQUE` constraint *bug
  fix* above is unrelated to the pivot — it's a correctness fix, not a schema
  redesign, and would have been needed under the PRAW path too.)
- Code generation, expiry, one-account-per-user enforcement.
- Verification-log-channel feature — unaffected; still reads the same columns.
- Threshold *values* (PLAN.md §4) — same numbers, now set in two places that must
  be kept in sync by hand: the Devvit app's settings (drives the actual decision)
  and the VPS `.env`'s `MIN_*` vars (only used for the log embed's "needs N+" text).

## What's retired

- `reddit_poller.py`, `praw`/`prawcore` from `requirements.txt`, and all the
  `REDDIT_CLIENT_ID`/`SECRET`/`USERNAME`/`PASSWORD`/`USER_AGENT` env vars — no
  inbox reading, no message parsing, no mark-as-read logic, no OAuth script-app
  credentials at all on this branch. Parked, not deleted: still present on
  `main`/`channelLogging` if this path is ever revived.
- `deploy/reddit_poller.service` — replaced by `deploy/webhook_receiver.service`.

## Explicitly out of scope for this pass

- Migrating `discord_bot.py` to Devvit — not applicable, same reasoning as v1/v2.
- The classic PRAW script-app path — parked, not abandoned, per Colby's decision to
  avoid the Data API special-access request process.
- Any per-user dynamic post creation — "Verify for Discord" is a single static
  pinned post, not generated per verification attempt.
- Adopting `getUserKarmaFromCurrentSubreddit()` as the subreddit-karma source of
  truth — implemented and confirmed callable, but not wired into `evaluate()` yet
  pending the self-user-exception nuance above being resolved with a second account.

## Testing notes — what's done, what's left

Done (this branch):
- Real `devvit playtest` run: form submit → `getCurrentUsername` → `getUserByUsername`
  → `getComments`/`getPosts` → `getUserKarmaFromCurrentSubreddit`, all succeeded,
  629ms total, real numbers logged and rendered in the UI.
- `npx tsc --build`, `npm run lint`, `npx vitest run`, `npm run build` all pass on
  `devvit/`.
- `webhook_receiver.py` exercised with FastAPI's `TestClient` end-to-end: auth
  rejection, expiry, idempotency (double-submit), username conflict (409), and a
  successful verdict write — all asserted against real `verify.db` rows, not mocks.
- `db.py`'s old-schema → new-schema migration tested against a simulated
  pre-existing database, confirming data survives and the constraint fix takes
  effect without manual intervention.
- Domain (`verify.verificationforyou.com`) + TLS + nginx live on the real VPS,
  confirmed end-to-end from the public internet: unauthenticated POST to
  `/devvit/verdict` → 401, authenticated POST with an unknown code → 400 —
  the full chain (DNS → TLS → nginx → `webhook_receiver.py` → auth/idempotency
  logic) works outside a lab. (Found and fixed along the way: certbot's nginx
  plugin appended its SSL block into the generic `default` site rather than a
  dedicated one, serving the domain as a static file root instead of proxying
  to the app — replaced with a proper dedicated site file, reusing the
  already-issued cert.)
- Devvit app uploaded and settings (`webhookUrl`, `webhookSecret`) set against
  the real, published app (not the throwaway spike one).
- Terms & Conditions / Privacy Policy pages published (`docs/` on `main`,
  GitHub Pages) and linked on Reddit's developer settings — required before
  `devvit publish` would even run for an app using the `http` plugin.
- `devvit publish` submitted successfully (version 0.0.2) — now pending human
  review (see Prerequisites #5 above).
- `discord_bot.py` connected to the real Discord server and got as far as
  posting/pinning the Verify message — required fixing real, live permission
  gaps: the bot's own role (`RedditVerificationBot`) had **no channel-specific
  overwrite at all** in `#verify-here`, so it inherited `@everyone`'s deny on
  `send_messages`/`manage_messages`/`embed_links`. Also needed **Pin
  Messages** specifically — Discord split pinning into its own permission bit
  separate from the older "Manage Messages," which `msg.pin()` requires.
- Ran the real flow against the dev subreddit (`r/verify_for_discor_dev`):
  clicked Verify in Discord → got the DM → opened the post → submitted the
  code. Confirmed live, for real, not just in the earlier spike:
  `reddit.getCurrentUsername()`, `getUserByUsername()`, the metrics
  computation, and `evaluate()` all ran successfully end to end. Only the
  final step — `postVerdict()`'s `fetch()` to the VPS — is currently blocked,
  and specifically by the review gate documented in Prerequisites #6, not by
  a bug in this code.

Still open — two separate review queues, not one:
- **Domain allowlist review** (Prerequisites #6) — blocks `postVerdict()`'s
  `fetch()` specifically. Documented turnaround: 1–2 business days typically.
  Check `https://developers.reddit.com/apps/verify-for-discord/developer-settings`
  for approval status.
- **"Creates custom posts" app review** (Prerequisites #5) — blocks
  `devvit install` on the real subreddit. No stated turnaround. Independent
  of the domain review above; may resolve on a different timeline.
  Everything else in the pipeline (Devvit identity/metrics side,
  `discord_bot.py`'s Discord-side handling, `webhook_receiver.py` itself) is
  now confirmed working independently of both queues.
- Confirm `getUserKarmaFromCurrentSubreddit()`'s moderator-gating with a second,
  non-mod Reddit account (PLAN.md §10 item 7) — can likely be combined with the
  hidden-profile mod-vs-non-mod test (item 6) since both need a second test account.
- `devvit install <subreddit>` on the real subreddit once approved, which
  triggers `onAppInstall` to create the pinned post — needed to get
  `DEVVIT_POST_URL` for the VPS `.env` and unblock starting `discord_bot.py`
  for real.
- A full live run all the way through: real Discord user clicks Verify → DMs
  code + post link → submits on Reddit → webhook → role assignment/DM →
  log-channel embed. Blocked on the review gate above; everything before and
  after that one gap is independently proven.
