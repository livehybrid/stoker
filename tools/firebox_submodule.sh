#!/usr/bin/env bash
# Check out the private firebox submodule (worker/engines/firebox) in CI using
# the read-only deploy key in FIREBOX_DEPLOY_KEY. Locally, `git submodule
# update --init worker/engines/firebox` with your own GitHub SSH key does the
# same thing. Exits 0 without the key so forks and PRs from outside still build
# (with the Python eventgen).
set -euo pipefail

if [ -z "${FIREBOX_DEPLOY_KEY:-}" ]; then
    echo "firebox_submodule: FIREBOX_DEPLOY_KEY not set; leaving the submodule empty (Python eventgen only)" >&2
    exit 0
fi

KEY_FILE="$(mktemp)"
trap 'shred -u "$KEY_FILE" 2>/dev/null || true' EXIT
printf '%s\n' "$FIREBOX_DEPLOY_KEY" > "$KEY_FILE"
chmod 600 "$KEY_FILE"
mkdir -p ~/.ssh
ssh-keyscan -t ed25519,rsa github.com >> ~/.ssh/known_hosts 2>/dev/null || true

export GIT_SSH_COMMAND="ssh -i $KEY_FILE -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
git submodule sync -- worker/engines/firebox
git submodule update --init --depth 1 -- worker/engines/firebox
echo "firebox submodule at $(git -C worker/engines/firebox rev-parse --short HEAD)"
