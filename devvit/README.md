# Verify for Discord (Devvit app)

The Reddit-hosted half of the verification bot — see
[../Claude/DEVVIT_PIVOT_SPEC.md](../Claude/DEVVIT_PIVOT_SPEC.md) for the full design
and [../README.md](../README.md) for how this fits with `discord_bot.py` and
`webhook_receiver.py`.

Built on Devvit's "Devvit Web" template: React 19 + Tailwind (client), Hono + tRPC
(server), running in Devvit's Node v22 serverless environment.

## What it does

A single pinned "Verify for Discord" post per subreddit. A user pastes the code
from their Discord DM into the on-page form; the server-side `verify.submit`
mutation resolves their Reddit identity for free (`reddit.getCurrentUsername()`),
pulls their account age/karma/subreddit history, evaluates it against this app's
settings, and POSTs the verdict to the VPS's `webhook_receiver.py`.

## Layout

- `src/client/splash.tsx` — the post UI: code input + Verify button.
- `src/server/trpc.ts` — the `verify.submit` mutation.
- `src/server/core/verify.ts` — metrics + threshold evaluation (mirrors what
  `reddit_poller.py` used to do with PRAW).
- `src/server/core/webhook.ts` — the POST to the VPS.
- `src/server/core/post.ts` + `src/server/routes/triggers.ts` — creates the pinned
  post automatically when the app is installed on a subreddit.
- `src/server/routes/menu.ts` — lets a moderator recreate the post on demand.

## Commands

```bash
npm install
npx devvit login
npx devvit playtest          # test against a throwaway dev subreddit first
npm run type-check           # tsc --build + lint + vitest
npm run build                # vite build
npm run deploy                # type-check, lint, test, then devvit upload
npx devvit publish           # publish for real, once ready
```

## Settings (not `.env` — set via `devvit settings set <key>`)

- `webhookUrl` — the VPS webhook endpoint, e.g. `https://verify.yourdomain.com/devvit/verdict`.
- `webhookSecret` — must match `DEVVIT_WEBHOOK_SECRET` in the VPS `.env`.
- `minAccountAgeDays`, `minTotalKarma`, `minSubredditActivityCount`, `minSubredditKarma`
  — the actual pass/fail thresholds. Keep these matching the `MIN_*` values in the
  VPS `.env` by hand (those only drive the verification-log-channel embed's text,
  not the real decision).

## Permissions

`devvit.json` requests `reddit` (moderator scope — needed for subreddit-specific
karma and PLAN.md §11's 28-day hidden-profile visibility) and `http` (to reach the
webhook — the target domain must be listed in `permissions.http.domains`).
