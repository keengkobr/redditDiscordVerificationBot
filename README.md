# redditDiscordVerificationBot

Gates a Discord server behind proof of active, non-burner Reddit membership. Full design rationale, thresholds, and roadmap live in [Claude/PLAN.md](Claude/PLAN.md) — this README is just setup/run instructions for the Phase 1 MVP implemented here.

## How it works

Two independent processes, talking only through a shared SQLite file (`verify.db`):

- **`discord_bot.py`** — posts the pinned Verify button in `#verify-here`, DMs users a pre-filled Reddit message link, and later polls the DB to assign the Verified role or send pass/fail DMs.
- **`reddit_poller.py`** — polls the bot account's Reddit inbox, matches codes, checks account age/karma/subreddit activity via PRAW, writes the verdict back to the DB.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
```

You'll need:

1. A **Discord bot application** (Discord Developer Portal) with the bot invited to your server, `Manage Roles` + `Send Messages` + `Read Message History` permissions, and the **Server Members Intent** enabled.
2. A **Reddit script app** ([reddit.com/prefs/apps](https://www.reddit.com/prefs/apps)) plus a dedicated bot Reddit account, made a **moderator** of your subreddit (see PLAN.md Section 11 — needed for hidden-profile visibility).
3. IDs for your guild, `#verify-here` channel, `#verify-review` mod channel, and the "Verified" role (enable Developer Mode in Discord to copy IDs).

Fill all of the above into `.env`.

## Running

```bash
python3 discord_bot.py      # process 1
python3 reddit_poller.py    # process 2, separate terminal/service
```

Both processes call `db.init_db()` on startup, so the schema is created automatically on first run — no migration step needed.

## Deploying to a VPS

Push this repo to GitHub, then on a fresh Ubuntu/Debian VPS (Section 6 of the plan — Hetzner CX22, DigitalOcean droplet, etc.):

```bash
git clone git@github.com:keengkobr/redditDiscordVerificationBot.git
sudo bash redditDiscordVerificationBot/deploy/install.sh
```

`deploy/install.sh` is idempotent and handles everything: system packages, a dedicated `botuser` system account, cloning into `/opt/redditDiscordVerificationBot`, a venv with dependencies installed, a starter `.env` (copied from `.env.example` if missing), and the two systemd services installed + enabled (not started, since `.env` still needs real credentials).

Once you've filled in `/opt/redditDiscordVerificationBot/.env`:

```bash
sudo systemctl start discord_bot reddit_poller
sudo systemctl status discord_bot reddit_poller
sudo journalctl -u discord_bot -f      # tail logs
sudo journalctl -u reddit_poller -f
```

To ship a code update later, push to GitHub then on the VPS run:

```bash
sudo bash /opt/redditDiscordVerificationBot/deploy/update.sh
```

which pulls, reinstalls dependencies, and restarts both services.

## Tuning thresholds

All the numbers from PLAN.md Section 4 (account age, karma, subreddit activity, code expiry) are environment variables — see `.env.example`. Change them without touching code.

## Status

Phase 1 (core verification) is implemented: button/DM flow, code matching, threshold checks, one-Reddit-account-per-Discord-account enforcement, plain-language pass/fail DMs, and a mod-review path for both soft-fails (likely hidden/curated profiles) and user-requested manual review. Phases 2-4 (roles/welcome/re-check, moderation, AI features) are not yet built — see PLAN.md Section 7 for the roadmap.
