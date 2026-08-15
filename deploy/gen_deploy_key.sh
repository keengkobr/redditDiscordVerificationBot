#!/usr/bin/env bash
# Standalone — does NOT require the repo to be cloned yet (chicken/egg fix
# for a private repo: run this first, authorize the key on GitHub, then clone).
#
# Generates an SSH keypair for root on this VPS and prints the public half
# to paste into GitHub: repo -> Settings -> Deploy keys -> Add deploy key
# (leave "Allow write access" UNCHECKED — read-only is all this needs).
#
# Usage: sudo bash gen_deploy_key.sh   (works even copy-pasted directly,
# no need to save it to a file first)

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root (e.g. 'sudo bash gen_deploy_key.sh')." >&2
    exit 1
fi

KEY_PATH="/root/.ssh/id_ed25519"

mkdir -p /root/.ssh
chmod 700 /root/.ssh

if [[ -f "${KEY_PATH}" ]]; then
    echo "==> Key already exists at ${KEY_PATH} — reusing it."
else
    echo "==> Generating a new ed25519 keypair at ${KEY_PATH}"
    ssh-keygen -t ed25519 -f "${KEY_PATH}" -N "" -C "redditDiscordVerificationBot-deploy-key"
fi

# Make sure git over SSH to github.com doesn't hang on an unknown host key.
ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
sort -u -o /root/.ssh/known_hosts /root/.ssh/known_hosts

echo
echo "==> Public key (paste this into GitHub -> repo -> Settings -> Deploy keys -> Add deploy key):"
echo
cat "${KEY_PATH}.pub"
echo
echo "Leave 'Allow write access' UNCHECKED (this VPS only needs to pull, never push)."
echo "Once added, re-run deploy/install.sh (or 'git clone' by hand) — the SSH clone will work."
