# redditDiscordVerificationBot

Gates a Discord server behind proof of active, non-burner Reddit membership. Full design rationale, thresholds, and roadmap live in [Claude/PLAN.md](Claude/PLAN.md) — this README is setup/run instructions for the current implementation.

**This branch uses Devvit, not PRAW/OAuth script apps** — see [Claude/DEVVIT_PIVOT_SPEC.md](Claude/DEVVIT_PIVOT_SPEC.md) for why. The classic script-app path (`reddit_poller.py`) is parked, not deleted — it's still on `main`/`channelLogging` if that path is ever revived.

## How it works

Two pieces — one on your VPS, one hosted on Reddit's own infrastructure — talking only through a shared SQLite file (`verify.db`) and a Discord Incoming Webhook:

- **`discord_bot.py`** (VPS) — posts the pinned Verify button in `#verify-here`, DMs users a code plus a link to the Reddit verification post, polls the DB to assign the Verified role/send pass-fail DMs/post log embeds, **and** owns a hidden relay channel where it creates a Discord Incoming Webhook and reads verdicts posted to it directly (`verdict.py`).
- **`devvit/`** (Reddit-hosted) — the pinned "Verify for Discord" post. Its form-submit handler resolves the submitting user's Reddit identity for free (no inbox, no OAuth), pulls their account age/karma/subreddit history, decides pass/fail, and POSTs the verdict to that Discord webhook.

There is **no self-hosted HTTP endpoint, no custom domain, no nginx/TLS** for the verdict hand-off. Reddit's [HTTP Fetch Policy](https://developers.reddit.com/docs) is explicit that personal/custom domains are never approved — only a fixed global allowlist skips review, and `discord.com` is on it. So instead of running our own webhook receiver, the Devvit app posts straight to a Discord webhook and `discord_bot.py` — already connected to the Discord gateway — reads it.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
```

You'll need:

1. A **Discord bot application** (Discord Developer Portal) with the bot invited to your server, `Manage Roles` + `Send Messages` + `Read Message History` + `Manage Webhooks` permissions, the **Server Members Intent**, and the **Message Content Intent** enabled (the latter is needed to read the relay webhook's messages).
2. A **hidden, bot-only Discord channel** (deny `@everyone` View Channel) for the webhook relay — `discord_bot.py` creates the actual webhook there on first startup; you just need the channel to exist and its ID in `.env`.
3. The **Devvit app** installed on your subreddit as a moderator (see "Devvit app" below) — moderator scope is what lets it read another user's subreddit-specific karma, and per PLAN.md Section 11 gives 28-day visibility into otherwise-hidden profile activity.
4. IDs for your guild, `#verify-here` channel, `#verify-review` mod channel, a `#verification-log` channel, the relay channel from #2, and the "Verified" role (enable Developer Mode in Discord to copy IDs). The bot needs `Send Messages` + `Embed Links` + `Pin Messages` in `#verify-here` (Discord split pinning into its own permission, separate from "Manage Messages").

Fill all of the above into `.env`.

## Running (VPS side)

```bash
python3 discord_bot.py
```

Just one process now. It calls `db.init_db()` on startup, so the schema is created (or migrated in place) automatically — no manual migration step needed. Watch its logs on first startup for a one-time-only line printing the relay webhook's URL — copy that into the Devvit app's `webhookUrl` setting immediately, since it's never logged again (it's the credential, same as an API secret).

## Devvit app

```bash
cd devvit
npm install
npx devvit login
npx devvit playtest              # test against a throwaway dev subreddit first
```

Once you're ready to go live on your real subreddit:

```bash
npx devvit settings set webhookUrl      # the Discord webhook URL discord_bot.py printed on first startup
npm run deploy                          # type-check, lint, test, then devvit upload
npx devvit publish
```

`webhookUrl` is the *only* setting needed for the hand-off now (no separate secret — the Discord webhook URL itself is the credential, and is marked `isSecret: true` in `devvit.json`). Note `devvit publish` is gated by Reddit's human review for any app that creates custom posts (this one does) — no stated turnaround; that's a separate queue from the domain allowlist, which doesn't apply here since `discord.com` needs no review.

Installing the app on your subreddit (as a moderator) auto-creates the pinned "Verify for Discord" post via its `onAppInstall` trigger. A moderator can also recreate it any time via the subreddit's mod-menu action of the same name.

Threshold values (`minAccountAgeDays`, `minTotalKarma`, `minSubredditActivityCount`, `minSubredditKarma`) are Devvit app settings, not `.env` — set them the same way as `webhookUrl` above. Keep them matching the `MIN_*` values in the VPS `.env` by hand — those are only used for the verification-log-channel embed's "needs N+" labels, not the actual pass/fail decision.

## Deploying the VPS side

This repo is **private**, so the VPS needs its own read-only credential before it can clone anything — a GitHub [deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys). On a fresh Ubuntu/Debian VPS (Section 6 of the plan — Hetzner CX22, DigitalOcean droplet, etc.):

**1. Generate and authorize a deploy key** (one-time, per VPS):

```bash
curl -fsSL https://raw.githubusercontent.com/keengkobr/redditDiscordVerificationBot/main/deploy/gen_deploy_key.sh | sudo bash
```

That fails the first time (it's a private repo — `raw.githubusercontent.com` can't serve it unauthenticated either). So instead, copy `deploy/gen_deploy_key.sh` to the VPS by hand — paste its contents into a file over SSH, or `scp deploy/gen_deploy_key.sh you@vps:~` from this machine — then:

```bash
sudo bash gen_deploy_key.sh
```

It prints a public key. Paste it into the GitHub repo → **Settings → Deploy keys → Add deploy key** (leave "Allow write access" unchecked — read-only is all this needs).

**2. Clone and install:**

```bash
git clone git@github.com:keengkobr/redditDiscordVerificationBot.git
sudo bash redditDiscordVerificationBot/deploy/install.sh
```

`deploy/install.sh` is idempotent and handles everything: system packages, a dedicated `botuser` system account, cloning into `/opt/redditDiscordVerificationBot` (over the SSH deploy key from step 1), a venv with dependencies installed, a starter `.env` (copied from `.env.example` if missing), and the `discord_bot` systemd service installed + enabled (not started, since `.env` still needs real credentials). No nginx/TLS/domain needed on this branch at all.

**3. Fill in `/opt/redditDiscordVerificationBot/.env`**, then:

```bash
sudo systemctl start discord_bot
sudo systemctl status discord_bot
sudo journalctl -u discord_bot -f       # tail logs -- watch for the relay webhook URL on first start
```

To ship a code update later, push to GitHub then on the VPS run:

```bash
sudo bash /opt/redditDiscordVerificationBot/deploy/update.sh
```

which pulls, reinstalls dependencies, and restarts the service. (This doesn't touch the Devvit app — redeploy that separately with `devvit publish` after code changes under `devvit/`.)

## Tuning thresholds

VPS-side numbers (code expiry, cooldown) are environment variables — see `.env.example`. The actual pass/fail thresholds live in the Devvit app's settings (see "Devvit app" above) — change them there without touching code.

## Status

Phase 1 (core verification) is implemented: button/DM flow, code matching, threshold checks (now computed by the Devvit app rather than a PRAW poller — see DEVVIT_PIVOT_SPEC.md), one-Reddit-account-per-Discord-account enforcement, plain-language pass/fail DMs, a mod-review path for both soft-fails (likely hidden/curated profiles) and user-requested manual review, and a verification log channel (see [Claude/VerificationLogChannel.md](Claude/VerificationLogChannel.md)) that posts an embed with the underlying numbers for every completed attempt. The verdict hand-off runs over a Discord webhook relay rather than a self-hosted endpoint (see DEVVIT_PIVOT_SPEC.md v4) — Reddit's HTTP Fetch Policy never approves personal/custom domains. Phases 2-4 (roles/welcome/re-check, moderation, AI features) are not yet built — see PLAN.md Section 7 for the roadmap.
