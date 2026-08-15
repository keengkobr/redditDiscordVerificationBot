#!/usr/bin/env bash
# Redeploy the latest code on the VPS after you've pushed changes to GitHub.
#
# Usage (on the VPS): sudo bash /opt/redditDiscordVerificationBot/deploy/update.sh

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/redditDiscordVerificationBot}"
SERVICE_USER="${SERVICE_USER:-botuser}"

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root (e.g. 'sudo bash deploy/update.sh')." >&2
    exit 1
fi

echo "==> Pulling latest code"
git -C "${INSTALL_DIR}" pull

echo "==> Updating Python dependencies"
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Restarting service"
systemctl restart discord_bot

echo "==> Done. Tail logs with: sudo journalctl -u discord_bot -f"
echo "    Note: this does not redeploy the Devvit app (devvit/) -- that's separate,"
echo "    run 'devvit publish' from a machine with the Devvit CLI after code changes there."
