# Verify for Discord

A Devvit app that verifies a Reddit account's age, karma, and subreddit
activity, then reports the result to a Discord server so it can grant (or
deny) a role. Built on Devvit's "Devvit Web" template: React 19 + Tailwind on
the client, Hono + tRPC on the server, running in Devvit's Node v22
serverless environment.

## What it does

A single pinned "Verify for Discord" post lives in the subreddit. A user
pastes a code (given to them by the Discord bot they're trying to verify
with) into the on-page form and hits Verify. The server-side `verify.submit`
mutation then:

1. Resolves the submitting user's Reddit identity for free
   (`reddit.getCurrentUsername()`) — no separate OAuth step.
2. Pulls their account age, total karma, and subreddit-specific post/comment
   count + karma.
3. Evaluates that against configurable thresholds (minimum account age,
   karma, activity).
4. Reports the pass/fail verdict — plus the underlying numbers — to a
   Discord Incoming Webhook.

Everything downstream (assigning a Discord role, DMing the user, logging the
attempt) happens on the Discord side once it receives that webhook call.

## Why a Discord webhook, not a custom server

Reddit's HTTP Fetch permission model only allows apps to call domains that
are either individually reviewed and approved, or already on a small global
allowlist that skips review entirely. Personal/custom domains are not
approved. `discord.com` is on the global allowlist, so posting straight to a
Discord webhook needs no domain review at all — a self-hosted server on a
custom domain would.

## Layout

- `src/client/splash.tsx` — the post UI: code input + Verify button.
- `src/server/trpc.ts` — the `verify.submit` mutation.
- `src/server/core/verify.ts` — account age/karma/subreddit-activity checks
  and threshold evaluation.
- `src/server/core/webhook.ts` — POSTs the verdict to the configured Discord
  webhook.
- `src/server/core/post.ts` + `src/server/routes/triggers.ts` — creates the
  pinned post automatically when the app is installed on a subreddit.
- `src/server/routes/menu.ts` — lets a moderator recreate the post on demand.

## Dev setup

```bash
npm install
npx devvit login
npx devvit playtest          # installs to a throwaway dev subreddit and streams logs
```

`playtest` rebuilds and reinstalls automatically whenever you save a file.
Test there before touching a real subreddit — anything under 200 subscribers
skips review entirely.

Once you're ready to install on a real subreddit:

```bash
npx devvit settings set webhookUrl   # a Discord Incoming Webhook URL
npm run deploy                        # type-check, lint, test, then devvit upload
npx devvit publish
```

`devvit publish` is subject to Reddit's human review for any app that
creates custom posts (this one does) before it can be installed anywhere
with more than 200 members.

## Settings

Set via `devvit settings set <key>`, not a `.env` file — these are stored by
Devvit at the app level, shared across every subreddit the app is installed on.

- `webhookUrl` (secret) — the full Discord Incoming Webhook URL. This is the
  only credential the app needs; the URL itself is the secret, so nothing
  else needs to be configured for the hand-off to work.
- `minAccountAgeDays` — minimum account age, in days. Default 30.
- `minTotalKarma` — minimum combined link + comment karma. Default 100.
- `minSubredditActivityCount` — minimum post/comment count in this
  subreddit. Default 5.
- `minSubredditKarma` — minimum karma earned in this subreddit. Default 20.

## Permissions

- `reddit`, moderator scope — needed to read subreddit-specific karma, and
  to retain visibility into a user's content for a window after they
  interact with the subreddit even if their profile is otherwise curated/hidden.
- `http`, `domains: ["discord.com"]` — the only domain this app ever calls.

## Fetch Domains

- `discord.com` — POSTs the computed verification verdict to a Discord
  Incoming Webhook so the receiving Discord bot can complete the
  verification (assign a role, DM the user, log the attempt). Globally
  pre-allowed by Reddit; no per-app review required.
