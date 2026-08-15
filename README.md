# redditDiscordVerificationBot

Gates a Discord server behind proof of active, non-burner Reddit membership. Full design rationale, thresholds, and roadmap live in [Claude/PLAN.md](Claude/PLAN.md) — this README is setup/run instructions for the current implementation.

**This branch uses Devvit, not PRAW/OAuth script apps** — see [Claude/DEVVIT_PIVOT_SPEC.md](Claude/DEVVIT_PIVOT_SPEC.md) for why. The classic script-app path (`reddit_poller.py`) is parked, not deleted — it's still on `main`/`channelLogging` if that path is ever revived.

## How it works

Three pieces, two of them on your VPS talking only through a shared SQLite file (`verify.db`), the third hosted on Reddit's own infrastructure:

- **`discord_bot.py`** (VPS) — posts the pinned Verify button in `#verify-here`, DMs users a code plus a link to the Reddit verification post, and polls the DB to assign the Verified role, send pass/fail DMs, and post a per-attempt embed to the verification log channel.
- **`devvit/`** (Reddit-hosted) — the pinned "Verify for Discord" post. Its form-submit handler resolves the submitting user's Reddit identity for free (no inbox, no OAuth), pulls their account age/karma/subreddit history, decides pass/fail, and POSTs the verdict to your VPS.
- **`webhook_receiver.py`** (VPS) — receives that POST, validates it, and writes the verdict into `verify.db`. Purely event-driven — no polling loop at all.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
```

You'll need:

1. A **Discord bot application** (Discord Developer Portal) with the bot invited to your server, `Manage Roles` + `Send Messages` + `Read Message History` permissions, and the **Server Members Intent** enabled.
2. A **domain pointed at the VPS**, with TLS via certbot — Devvit's `permissions.http.domains` allowlist requires HTTPS. See `deploy/nginx-verify.conf.example`.
3. The **Devvit app** installed on your subreddit as a moderator (see "Devvit app" below) — moderator scope is what lets it read another user's subreddit-specific karma, and per PLAN.md Section 11 gives 28-day visibility into otherwise-hidden profile activity.
4. IDs for your guild, `#verify-here` channel, `#verify-review` mod channel, a `#verification-log` channel, and the "Verified" role (enable Developer Mode in Discord to copy IDs). The bot needs `Send Messages` + `Embed Links` in the log channel.

Fill all of the above into `.env`.

## Running (VPS side)

```bash
python3 discord_bot.py
uvicorn webhook_receiver:app --host 127.0.0.1 --port 8000   # behind nginx, see below
```

Both processes call `db.init_db()` on startup, so the schema is created (or migrated in place) automatically — no manual migration step needed.

## Devvit app

```bash
cd devvit
npm install
npx devvit login
npx devvit playtest              # test against a throwaway dev subreddit first
```

Once you're ready to go live on your real subreddit:

```bash
npx devvit settings set webhookUrl      # https://verify.yourdomain.com/devvit/verdict
npx devvit settings set webhookSecret   # must match DEVVIT_WEBHOOK_SECRET in the VPS .env
npm run deploy                          # type-check, lint, test, then devvit upload
npx devvit publish
```

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

`deploy/install.sh` is idempotent and handles everything: system packages, a dedicated `botuser` system account, cloning into `/opt/redditDiscordVerificationBot` (over the SSH deploy key from step 1), a venv with dependencies installed, a starter `.env` (copied from `.env.example` if missing), and the `discord_bot`/`webhook_receiver` systemd services installed + enabled (not started, since `.env` still needs real credentials).

It does **not** set up nginx/TLS or the Devvit app — those are separate, deliberately manual steps (see `deploy/nginx-verify.conf.example` and "Devvit app" above), since they need your actual domain name and Reddit login.

**3. Set up nginx + TLS**, then fill in `/opt/redditDiscordVerificationBot/.env`, then:

```bash
sudo systemctl start discord_bot webhook_receiver
sudo systemctl status discord_bot webhook_receiver
sudo journalctl -u discord_bot -f       # tail logs
sudo journalctl -u webhook_receiver -f
```

To ship a code update later, push to GitHub then on the VPS run:

```bash
sudo bash /opt/redditDiscordVerificationBot/deploy/update.sh
```

which pulls, reinstalls dependencies, and restarts both services. (This doesn't touch the Devvit app — redeploy that separately with `devvit publish` after code changes under `devvit/`.)

## Tuning thresholds

VPS-side numbers (code expiry, cooldown) are environment variables — see `.env.example`. The actual pass/fail thresholds live in the Devvit app's settings (see "Devvit app" above) — change them there without touching code.

## Status

Phase 1 (core verification) is implemented: button/DM flow, code matching, threshold checks (now computed by the Devvit app rather than a PRAW poller — see DEVVIT_PIVOT_SPEC.md), one-Reddit-account-per-Discord-account enforcement, plain-language pass/fail DMs, a mod-review path for both soft-fails (likely hidden/curated profiles) and user-requested manual review, and a verification log channel (see [Claude/VerificationLogChannel.md](Claude/VerificationLogChannel.md)) that posts an embed with the underlying numbers for every completed attempt. Phases 2-4 (roles/welcome/re-check, moderation, AI features) are not yet built — see PLAN.md Section 7 for the roadmap.
