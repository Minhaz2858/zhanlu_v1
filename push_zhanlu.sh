#!/bin/bash
# One-command push for zhanlu_v1 — run AFTER fixing GitHub auth (see README note).
set -e
cd "$(dirname "$0")"

# If SSH key is registered (option A), this just works:
if ssh -T -o ConnectTimeout=8 -o BatchMode=yes git@github.com 2>&1 | grep -q "successfully authenticated"; then
  git push -u origin main --force
  echo "PUSHED via SSH"
  exit 0
fi

# Otherwise use a keychain token. Priority:
#   1. GitHub Desktop OAuth token (gho_...) — generic password, has full repo write
#   2. Fine-grained PAT (github_pat_...) — only works if given Contents: write on the repo
TOKEN=$(security find-generic-password -s "GitHub - https://api.github.com" -a Minhaz2858 -w 2>/dev/null)
if [ -z "$TOKEN" ]; then
  TOKEN=$(security find-internet-password -s github.com -a Minhaz2858 -w 2>/dev/null)
fi
if [ -z "$TOKEN" ]; then
  echo "No GitHub credential found in keychain. Open GitHub Desktop once to refresh the login, then rerun."
  exit 1
fi
printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > /tmp/gh-cred.tmp
GIT_CONFIG_SYSTEM=/dev/null git -c http.proxy=http://127.0.0.1:7897 \
  -c credential.helper="store --file=/tmp/gh-cred.tmp" \
  push -u origin main --force
rm -f /tmp/gh-cred.tmp
echo "PUSHED via HTTPS token"
