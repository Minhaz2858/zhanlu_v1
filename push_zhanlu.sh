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

# Otherwise use the keychain fine-grained token (option B: token must have Contents: write on zhanlu_v1)
TOKEN=$(security find-internet-password -s github.com -a Minhaz2858 -w 2>/dev/null)
if [ -z "$TOKEN" ]; then
  echo "No GitHub credential found. Register the SSH key (github.com/settings/ssh/new) or fix the token, then rerun."
  exit 1
fi
printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > /tmp/gh-cred.tmp
GIT_CONFIG_SYSTEM=/dev/null git -c http.proxy=http://127.0.0.1:7897 \
  -c credential.helper="store --file=/tmp/gh-cred.tmp" \
  push -u origin main --force
rm -f /tmp/gh-cred.tmp
echo "PUSHED via HTTPS token"
