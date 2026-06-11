#!/usr/bin/env bash
# Race-safe commit & push of queue/ (+ optional extra state files) to origin/main.
#
# Several scheduled or manually-triggered workflows can write queue/index.json at
# the same time. Item payload files are uniquely named (never conflict); only
# index.json does. Since index.json is fully derivable from the item files, on any
# push race we rebase onto origin/main and, if index.json conflicts, rebuild it
# from the union of item files and continue. A retry loop absorbs the push race.
#
# Usage: scripts/sync_queue.sh "<commit message>" [extra git-add path]...
set -uo pipefail

MSG="${1:?cal un missatge de commit}"; shift || true

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

git add -f queue/ 2>/dev/null || true
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
  if ! git rebase origin/main; then
    echo "  Conflicte (segurament queue/index.json) → reconstrueixo l'índex des dels items."
    python -c "import queue_store; print('items:', queue_store.rebuild_index())"
    git add -A queue/
    GIT_EDITOR=true git rebase --continue || { git rebase --abort || true; echo "  rebase avortat, reintento"; }
  fi
  sleep $(( (RANDOM % 4) + 2 ))
done

echo "❌ No s'ha pogut fer push després de diversos reintents."
exit 1
