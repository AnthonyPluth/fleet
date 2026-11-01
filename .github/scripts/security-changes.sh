#!/usr/bin/env bash
# Which scans a change can affect, for the `changes` job of security.yml. Writes `code`, `web_deps`, `image` and
# `workflows` (true/false) to $GITHUB_OUTPUT. A pull request's files come from the files API, a push's from the
# comparison with its `before` commit; a scheduled or manual run, or a change too big to list, scans everything.
# A change under .github/ runs every scan. A rename counts for both its old and its new path.
#
# Environment: CODE_PATHS (regex; Semgrep's paths), WEB_DIRS (newline list of directories with a package-lock.json;
# empty: no npm audit), IMAGE_PATHS (regex; what the image holds beyond the app's own code; empty: no image scan),
# REPO, EVENT, PR, BEFORE, AFTER, GH_TOKEN, GITHUB_OUTPUT.
set -euo pipefail

: "${CODE_PATHS:?CODE_PATHS (a regex) is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
WEB_DIRS=${WEB_DIRS:-}
IMAGE_PATHS=${IMAGE_PATHS:-}
REPO=${REPO:-}
EVENT=${EVENT:-}
PR=${PR:-}
BEFORE=${BEFORE:-}
AFTER=${AFTER:-}

paths='.filename, (.previous_filename // empty)'
if [ -n "$PR" ]; then
  files=$(gh api --paginate "repos/$REPO/pulls/$PR/files?per_page=100" | jq -r ".[] | $paths")
  limit=3000      # the most the files API lists
elif [ "$EVENT" = push ] && [[ "$BEFORE" =~ ^[0-9a-f]{40}$ ]] && [ "$BEFORE" != 0000000000000000000000000000000000000000 ]; then
  files=$(gh api "repos/$REPO/compare/$BEFORE...$AFTER" | jq -r ".files[] | $paths")
  limit=300       # the most a comparison lists
else
  files=".github/(a scheduled or manual run scans everything)"
  limit=0
fi
if [ "$limit" -gt 0 ] && [ "$(grep -c . <<< "$files" || true)" -ge "$limit" ]; then
  files=".github/(too many to list)"
fi

touches() {
  if grep -qE "$1|^\.github/" <<< "$files"; then echo true; else echo false; fi
}

# The web directories as one regex alternation of their escaped names: `frontend` → ^(frontend)/package(-lock)?\.json$
web_pattern=""
while IFS= read -r dir; do
  dir=${dir%/}
  [ -n "$dir" ] || continue
  escaped=$(printf '%s' "$dir" | sed -e 's/[][\.|$(){}?+*^\\/]/\\&/g')
  web_pattern="${web_pattern:+$web_pattern|}$escaped"
done <<< "$WEB_DIRS"

{
  echo "code=$(touches "$CODE_PATHS")"
  if [ -n "$web_pattern" ]; then echo "web_deps=$(touches "^($web_pattern)/package(-lock)?\\.json$")"; else echo "web_deps=false"; fi
  if [ -n "$IMAGE_PATHS" ]; then echo "image=$(touches "$IMAGE_PATHS")"; else echo "image=false"; fi
  echo "workflows=$(touches '^\.github/')"
} | tee -a "$GITHUB_OUTPUT"
