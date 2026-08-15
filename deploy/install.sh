#!/usr/bin/env bash
# Bootstrap script for a fresh Ubuntu/Debian VPS (PLAN.md Section 6).
#
# Usage (run as root, or a user with sudo, on the VPS itself):
#   curl -fsSL https://raw.githubusercontent.com/keengkobr/redditDiscordVerificationBot/main/deploy/install.sh | sudo bash
# or, if you've already cloned the repo:
#   sudo bash deploy/install.sh
#
# What it does:
#   1. apt-installs python3/venv/pip/git
#   2. creates a dedicated unprivileged system user to run the bot
#   3. clones (or updates) the repo into /opt/redditDiscordVerificationBot
#   4. creates a venv and pip-installs requirements.txt
#   5. drops a .env from .env.example if one doesn't exist yet (you MUST edit it)
#   6. installs + enables (but does not start) the two systemd services
#
# Safe to re-run: it's idempotent (git pull instead of re-clone, etc).

set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:keengkobr/redditDiscordVerificationBot.git}"
REPO_URL_HTTPS="${REPO_URL_HTTPS:-https://github.com/keengkobr/redditDiscordVerificationBot.git}"
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
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    git -C "${INSTALL_DIR}" pull
else
    mkdir -p "${INSTALL_DIR}"
    # Try SSH remote first (works if this box has a deploy key); fall back to HTTPS
    # (works for a public repo with no auth needed).
    git clone "${REPO_URL}" "${INSTALL_DIR}" 2>/dev/null \
        || git clone "${REPO_URL_HTTPS}" "${INSTALL_DIR}"
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
sed "s#/opt/redditDiscordVerificationBot#${INSTALL_DIR}#g; s#botuser#${SERVICE_USER}#g" \
    "${INSTALL_DIR}/deploy/reddit_poller.service" > /etc/systemd/system/reddit_poller.service

systemctl daemon-reload
systemctl enable discord_bot reddit_poller

echo
echo "==> Done."
echo "    1. Edit ${INSTALL_DIR}/.env with real credentials/IDs."
echo "    2. Start everything with:"
echo "         sudo systemctl start discord_bot reddit_poller"
echo "    3. Check status/logs with:"
echo "         sudo systemctl status discord_bot reddit_poller"
echo "         sudo journalctl -u discord_bot -f"
echo "         sudo journalctl -u reddit_poller -f"
