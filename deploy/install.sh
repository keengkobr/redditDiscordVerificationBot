#!/usr/bin/env bash
# Bootstrap script for a fresh Ubuntu/Debian VPS (PLAN.md Section 6).
#
# This repo is PRIVATE, so cloning needs an SSH deploy key set up on the VPS
# first — see deploy/gen_deploy_key.sh, or the README "Deploying to a VPS"
# section. Once that's done:
#
#   sudo bash deploy/install.sh
#
# (or, if you haven't cloned yet but the deploy key is already authorized:
#   git clone git@github.com:keengkobr/redditDiscordVerificationBot.git
#   sudo bash redditDiscordVerificationBot/deploy/install.sh )
#
# What it does:
#   1. apt-installs python3/venv/pip/git
#   2. creates a dedicated unprivileged system user to run the bot
#   3. clones (or updates) the repo into /opt/redditDiscordVerificationBot
#      over SSH using root's key (see gen_deploy_key.sh)
#   4. creates a venv and pip-installs requirements.txt
#   5. drops a .env from .env.example if one doesn't exist yet (you MUST edit it)
#   6. installs + enables (but does not start) the discord_bot systemd service
#
# NOT covered here (do this separately):
#   - the Devvit app itself (see devvit/) -- deployed via `devvit publish`, not this script
#
# No nginx/TLS/custom domain needed on this branch (DEVVIT_PIVOT_SPEC.md v4) --
# the Devvit app posts its verdict to a Discord Incoming Webhook (discord.com
# is globally pre-allowed by Reddit's HTTP Fetch Policy; personal/custom
# domains are never approved), and discord_bot.py reads that channel directly.
#
# Safe to re-run: it's idempotent (git pull instead of re-clone, etc).

set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:keengkobr/redditDiscordVerificationBot.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/redditDiscordVerificationBot}"
SERVICE_USER="${SERVICE_USER:-botuser}"

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root (e.g. 'sudo bash deploy/install.sh')." >&2
    exit 1
fi

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

echo "==> Ensuring service user '${SERVICE_USER}' exists"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Fetching code into ${INSTALL_DIR}"
# This script runs as root, but the repo ends up owned by ${SERVICE_USER}
# (see chown below) -- git refuses to operate on a repo it doesn't own unless
# told it's safe (CVE-2022-24765 mitigation). Without this, a re-run after the
# first chown fails with "detected dubious ownership".
git config --global --add safe.directory "${INSTALL_DIR}"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
    git -C "${INSTALL_DIR}" pull
else
    mkdir -p "${INSTALL_DIR}"
    # Private repo — this needs an SSH deploy key already authorized on GitHub
    # for root's key (see deploy/gen_deploy_key.sh). GIT_SSH_COMMAND disables
    # interactive host-key/password prompts so a missing key fails fast with a
    # clear error instead of hanging.
    if ! GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
        git clone "${REPO_URL}" "${INSTALL_DIR}"; then
        echo
        echo "Clone failed — this VPS likely doesn't have an authorized deploy key yet." >&2
        echo "Run deploy/gen_deploy_key.sh on this VPS, add the printed public key as a" >&2
        echo "read-only Deploy Key on the GitHub repo, then re-run this script." >&2
        exit 1
    fi
fi

echo "==> Creating virtualenv + installing Python dependencies"
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    echo "==> No .env found — copying .env.example. YOU MUST EDIT THIS BEFORE STARTING THE SERVICES."
    cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
    chmod 600 "${INSTALL_DIR}/.env"
fi

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Installing systemd units"
sed "s#/opt/redditDiscordVerificationBot#${INSTALL_DIR}#g; s#botuser#${SERVICE_USER}#g" \
    "${INSTALL_DIR}/deploy/discord_bot.service" > /etc/systemd/system/discord_bot.service

systemctl daemon-reload
systemctl enable discord_bot

echo
echo "==> Done."
echo "    1. Edit ${INSTALL_DIR}/.env with real credentials/IDs, including"
echo "       VERIFY_RELAY_CHANNEL_ID (a hidden, bot-only channel) and publish"
echo "       the Devvit app (see devvit/) with its webhookUrl setting pointed"
echo "       at the Discord Incoming Webhook discord_bot.py creates there on"
echo "       first startup (watch the logs below for the one-time URL)."
echo "    2. Start it with:"
echo "         sudo systemctl start discord_bot"
echo "    3. Check status/logs with:"
echo "         sudo systemctl status discord_bot"
echo "         sudo journalctl -u discord_bot -f"
