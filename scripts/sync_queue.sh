#!/usr/bin/env bash
# Race-safe commit & push of optional STATE files (history, posts) to origin/main.
#
# The Reddit post queue NO LONGER lives in git — it goes to the private Cloudflare
# Worker (queue_store.enqueue → POST /enqueue). So this script only syncs the extra
# state files passed as arguments (e.g. output/history.json, output/posts/*.md).
# If no extra paths are given (e.g. the SX3 workflow, which only ever wrote the
# queue), there is nothing to commit and it exits cleanly.
#
# Usage: scripts/sync_queue.sh "<commit message>" [extra git-add path]...
set -uo pipefail

MSG="${1:?cal un missatge de commit}"; shift || true

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

for p in "$@"; do
  # shellcheck disable=SC2086  (intentional glob expansion, e.g. output/posts/*.md)
  git add -f $p 2>/dev/null || true
done

if git diff --cached --quiet; then
  echo "Res nou a desar."
  exit 0
fi
git commit -m "$MSG" --quiet

for attempt in 1 2 3 4 5; do
  if git push --quiet origin HEAD:main; then
    echo "✅ Push correcte (intent $attempt)."
    exit 0
  fi
  echo "↻ Push rebutjat; sincronitzant amb origin/main (intent $attempt)…"
  git fetch --quiet origin main || true
  git rebase origin/main || { git rebase --abort || true; echo "  rebase avortat, reintento"; }
  sleep $(( (RANDOM % 4) + 2 ))
done

echo "❌ No s'ha pogut fer push després de diversos reintents."
exit 1
