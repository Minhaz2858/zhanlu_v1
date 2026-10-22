#!/bin/bash
# publish_snapshot.sh — publish the CURRENT WORKING TREE of this repo to
# https://github.com/Minhaz2858/zhanlu_v1 as a source-only snapshot commit.
#
# Why a snapshot and not a plain `git push`:
#   The dev history tracks 1.4 GB / 91k files including third-party dumps
#   (vendor/, deepseek-harness-master/, echarts-master/). GitHub does not want
#   that, and the public repo is deliberately source-only. So we export the
#   working tree (committed + uncommitted) into a scratch clone of the public
#   repo, commit once, and push.
#
# Auth: the GitHub Desktop OAuth token in the login keychain
#   (service "GitHub - https://api.github.com"). It carries `workflow` scope,
#   which `osxkeychain` credentials for github.com do NOT — a push touching
#   .github/workflows/* fails with "refusing to allow an OAuth App to create or
#   update workflow ... without `workflow` scope" if you use the plain helper.
#
# Usage:  scripts/publish_snapshot.sh [--dry-run]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_URL="https://github.com/Minhaz2858/zhanlu_v1.git"
WORK="$(mktemp -d /tmp/zhanlu_v1_snapshot.XXXXXX)"
BRANCH="main"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# Files present on the public repo but absent (or unwanted) here — never synced,
# never deleted by rsync --delete.
PROTECT="README.md LICENSE push_zhanlu.sh"

# Everything that is not source: third-party dumps, generated deliverables,
# secrets, local state.
EXCLUDES=(
  .git .git/ node_modules/ vendor/ deepseek-harness-master/ echarts-master/
  backups/ data/ backend/outputs/ agent_qa/ outputs/ .worktrees/ logs/
  __pycache__/ '*.pyc' '*.pyo' '*.db' '*.sqlite' '*.zip' '*.dmg' '*.log'
  .DS_Store .env .env.production backend/.env 'backend/.env.*'
  backend/venv/ 'backend/venv.*/' .venv/ .idea/ .vscode/ .ruff_cache/
  .pytest_cache/ '*.egg-info/'
)
EXCL_FILE="$WORK/.excludes"
: > "$EXCL_FILE"
for e in "${EXCLUDES[@]}" $PROTECT; do printf '%s\n' "$e" >> "$EXCL_FILE"; done

echo "==> cloning $REMOTE_URL"
git clone -q "$REMOTE_URL" "$WORK/publish"

echo "==> exporting working tree (source only)"
rsync -a --delete --exclude-from="$EXCL_FILE" "$REPO_DIR/" "$WORK/publish/"

cd "$WORK/publish"
if [ -z "$(git status --porcelain)" ]; then
  echo "Nothing to publish — remote already matches the working tree."
  exit 0
fi
git add -A
echo "==> changes"
git status --short | head -40

STAMP="$(date +%Y-%m-%d)"
git -c user.name="Minhazul Islam" -c user.email="22351396@zju.edu.cn" commit -q \
  -m "Snapshot: Zhanlu platform source ($STAMP)

Source-only export of the platform working tree ($(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD) @ $(git -C "$REPO_DIR" rev-parse --short HEAD) + working-tree changes)." 

if [ "$DRY" = "1" ]; then
  echo "==> dry run: commit $(git rev-parse --short HEAD) created, not pushed"
  exit 0
fi

TOKEN="$(security find-generic-password -s "GitHub - https://api.github.com" -w 2>/dev/null || true)"
if [ -z "$TOKEN" ]; then
  echo "No GitHub Desktop token in keychain. Open GitHub Desktop once, then rerun." >&2
  exit 1
fi
# Token in URL (not the remote config) — the osxkeychain helper lacks workflow
# scope and the .git-credentials store format 403s.
git -c credential.helper= push "https://x:${TOKEN}@github.com/Minhaz2858/zhanlu_v1.git" "$BRANCH"
echo "==> pushed $(git rev-parse --short HEAD) to $REMOTE_URL ($BRANCH)"
