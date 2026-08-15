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
#   6. installs + enables (but does not start) the discord_bot and
#      webhook_receiver systemd services
#
# NOT covered here (DEVVIT_PIVOT_SPEC.md prerequisites -- do these separately):
#   - nginx + TLS in front of webhook_receiver (see deploy/nginx-verify.conf.example)
#   - the Devvit app itself (see devvit/) -- deployed via `devvit publish`, not this script
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
sed "s#/opt/redditDiscordVerificationBot#${INSTALL_DIR}#g; s#botuser#${SERVICE_USER}#g" \
    "${INSTALL_DIR}/deploy/webhook_receiver.service" > /etc/systemd/system/webhook_receiver.service

systemctl daemon-reload
systemctl enable discord_bot webhook_receiver

echo
echo "==> Done."
echo "    1. Edit ${INSTALL_DIR}/.env with real credentials/IDs."
echo "    2. Set up nginx + TLS in front of webhook_receiver (see"
echo "       deploy/nginx-verify.conf.example) and publish the Devvit app (see devvit/)."
echo "    3. Start everything with:"
echo "         sudo systemctl start discord_bot webhook_receiver"
echo "    4. Check status/logs with:"
echo "         sudo systemctl status discord_bot webhook_receiver"
echo "         sudo journalctl -u discord_bot -f"
echo "         sudo journalctl -u webhook_receiver -f"
