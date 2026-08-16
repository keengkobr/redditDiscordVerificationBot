# Developing this app

Developer-facing notes for working on the code itself. For what the app does and how to install
it on a subreddit, see [README.md](README.md).

Built on Devvit's "Devvit Web" template: React 19 + Tailwind on the client, Hono + tRPC on the
server, running in Devvit's Node v22 serverless environment.

## Why a Discord webhook, not a custom server

Reddit's HTTP Fetch permission model only allows apps to call domains that are either
individually reviewed and approved, or already on a small global allowlist that skips review
entirely. Personal/custom domains are not approved. `discord.com` is on the global allowlist, so
posting straight to a Discord webhook needs no domain review at all — a self-hosted server on a
custom domain would.

## Layout

- `src/client/splash.tsx` — the post UI: code input + Verify button + pass/fail requirements checklist.
- `src/server/trpc.ts` — the `verify.submit` mutation.
- `src/server/core/verify.ts` — claim decoding, identity/karma/activity checks, threshold
  evaluation, and the Redis-backed anti-duplicate check.
- `src/server/core/webhook.ts` — POSTs the verdict to the configured Discord webhook.
- `src/server/core/post.ts` + `src/server/routes/triggers.ts` — creates the pinned post
  automatically when the app is installed on a subreddit.
- `src/server/routes/menu.ts` — lets a moderator recreate the post on demand.

See [../Claude/DEVVIT_PIVOT_SPEC.md](../Claude/DEVVIT_PIVOT_SPEC.md) for the full design history
and rationale (why there's no database, how the claim/match mechanism works, etc.).

## Dev setup

```bash
npm install
npx devvit login
npx devvit playtest          # installs to a throwaway dev subreddit and streams logs
```

`playtest` rebuilds and reinstalls automatically whenever you save a file. Test there before
touching a real subreddit — anything under 200 subscribers skips review entirely.

Once you're ready to install on a real subreddit:

```bash
npm run deploy    # type-check, lint, test, then devvit upload
npx devvit publish
```

`devvit publish` is subject to Reddit's human review for any app that creates custom posts (this
one does) before it can be installed anywhere with more than 200 members. Settings (webhookUrl and
the thresholds) are configured per-install after that, not via a CLI command -- see "Settings" below.

## Settings

All settings are **subreddit-scoped** (`devvit.json`'s `settings.subreddit`), not global -- each
subreddit that installs this app configures its own copy, since each one talks to a different
Discord server. That also means the `devvit settings set`/`list` CLI commands **don't work for
these at all** (that CLI is explicitly global-scope only, per `devvit settings --help`). Instead,
each install's moderators set them via that subreddit's own Mod Tools -> Apps -> Verify for
Discord -> Settings page.

One consequence: subreddit-scoped string settings can't be marked `isSecret` (a Devvit schema
limitation -- that flag only exists for global settings), so `webhookUrl` is **not masked** on
that settings page. Each subreddit's mods see their own value in plaintext, which is an accepted
tradeoff for self-service multi-tenant setup -- it's their own credential, visible only to their
own mods with settings access, never to anyone outside that subreddit.

- `webhookUrl` — the full Discord Incoming Webhook URL, specific to that Discord server. Get it by
  running `get_relay_webhook_url.py` on that server's Discord bot host.
- `minAccountAgeDays` — minimum account age, in days. Default 30.
- `minTotalKarma` — minimum combined link + comment karma. Default 50.
- `minSubredditActivityCount` — minimum post/comment count in this subreddit. Default 1.
- `minSubredditKarma` — minimum karma earned in this subreddit. Default 50.

For local dev/playtest, set these the same way -- on the dev subreddit's own Mod Tools -> Apps ->
Settings page (`playtest` installs to `devvit.json`'s `dev.subreddit`), not via the CLI.

## Permissions

- `reddit`, moderator scope — needed to read subreddit-specific karma, and to retain visibility
  into a user's content for a window after they interact with the subreddit even if their
  profile is otherwise curated/hidden.
- `http`, `domains: ["discord.com"]` — the only domain this app ever calls.

## Fetch Domains

- `discord.com` — POSTs the computed verification verdict to a Discord Incoming Webhook so the
  receiving Discord bot can complete the verification (assign a role, DM the user, log the
  attempt). Globally pre-allowed by Reddit; no per-app review required.
