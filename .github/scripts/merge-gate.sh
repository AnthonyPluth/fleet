#!/usr/bin/env bash
# The merge gate: one commit status ("Merge gate") on a pull request's head that says whether it's ready to merge, so a
# ruleset on main can require that one check instead of a list that goes stale. Run by the reusable workflow
# .github/workflows/merge-gate.yml each time one of a pull request's workflows finishes, from the caller's main copy
# (it never runs a pull request's code). This is fleet's one copy of the script Waypoint and Runway each carried; the
# parts that differed between them are configuration (below).
#
# It passes when, on the pull request's head commit:
#   - the newest run of every workflow started by the pull request succeeded (or was skipped), and the ones in
#     REQUIRED_WORKFLOWS ran at all. CodeQL counts as one of them: GitHub's default setup runs it as a "dynamic" workflow
#     (not a pull_request one), under the name "PR #<n>";
#   - every other app's check run (CodeQL, Semgrep, Trivy, zizmor, ...) succeeded, and every commit status did, and the
#     ones in REQUIRED_STATUSES were set at all. The default is "Second review" alone (the owner's policy): the second
#     review fails on a risky pull request with a blocking finding and passes on any other saying "Not a risky pull
#     request". "Agent review", the independent first review, is required only where an app turns it on and lists it;
#     like any status it fails the gate when it fails, required or not. Each must be on the head commit itself: when a
#     review copies a passing verdict forward (a push that only merged main in), it sets the status on the new head,
#     and a head without one waits.
#   - no review thread whose first comment has a blocking badge (P1 or P2 by default, from Codex by default) is still
#     open: resolving the thread clears it, and the caller's merge-gate-threads workflow runs the gate again when that
#     happens.
#   - with REQUIRE_CODEX_REVIEW, Codex has reviewed the head commit itself (not an older one): it waits until then.
# Anything still running makes it "pending". Workflows and apps that aren't about whether the change is sound are left
# out (IGNORED_*). When it passes on one of Dependabot's pull requests, it merges it (see the end).
#
# CodeQL's finishing starts no workflow here (GitHub sends no event for its own Actions runs), so when CodeQL is all
# it's waiting for, it looks again every POLL_SECONDS, up to POLL_ATTEMPTS times, rather than stay pending.
#
# Needs: GH_TOKEN, REPO (owner/name), and SHA (the head commit) or PR (a number). RUN_URL links the status to the run.
# Configuration, each set by the reusable workflow from its inputs (an unset variable takes the default shown; an empty
# one means none):
#   REQUIRED_WORKFLOWS   workflows that must have run, one per line        (default: Security scans, CodeQL)
#   IGNORED_WORKFLOWS    workflow names to leave out, as a regex alternation ("Preview|Screenshot diff");
#                        "Cancel runs on close" and "Merge gate" are always left out
#   REQUIRED_STATUSES    commit statuses that must be set, one per line    (default: Second review)
#   IGNORED_APPS         apps whose check runs don't count, as a regex alternation (default: codecov|github-actions)
#   POLL_SECONDS, POLL_ATTEMPTS   how long to wait for CodeQL             (default: 30, 20)
#   BLOCKING_THREAD_BADGES badges on a review thread's first comment that block the merge while the thread is open,
#                        as a regex alternation; empty turns the check off (default: P1|P2)
#   BLOCKING_THREAD_AUTHORS logins whose first comment counts, as a regex alternation without a "[bot]" suffix; empty
#                        lets any author's badge count (default: chatgpt-codex-connector)
#   REQUIRE_CODEX_REVIEW wait for a completed Codex review of the head (not on Dependabot's pull requests): a review
#                        by chatgpt-codex-connector[bot] on the head commit, or its comment whose "Reviewed commit" or
#                        review state names it (default: false)
#   AUTOMERGE_DEPENDABOT merge Dependabot's pull requests once the gate passes (default: true)
#   AFTER_MERGE_WORKFLOWS workflow files to start on main after such a merge, space-separated
#                        (default: docker.yml security.yml)
# DRY_RUN=1 (or true) prints what it would do instead of doing it.
set -euo pipefail

CONTEXT="Merge gate"
required_workflows_text=${REQUIRED_WORKFLOWS-$'Security scans\nCodeQL'}   # take CodeQL out if its default setup is turned off
required_statuses_text=${REQUIRED_STATUSES-Second review}   # Dependabot's pull requests aren't reviewed (below)
IGNORED_WORKFLOWS="^(Cancel runs on close|Merge gate${IGNORED_WORKFLOWS:+|$IGNORED_WORKFLOWS})$"
IGNORED_APPS="^(${IGNORED_APPS-codecov|github-actions})$"   # Codecov's statuses are informational; Actions' runs are counted above
IGNORED_STATUSES="^(${CONTEXT}|codecov/.*)$"
OK_CONCLUSIONS='^(success|skipped|neutral)$'
POLLED='^CodeQL( \(not started\))?$'
POLL_SECONDS=${POLL_SECONDS:-30}
POLL_ATTEMPTS=${POLL_ATTEMPTS:-20}
BLOCKING_THREAD_BADGES=${BLOCKING_THREAD_BADGES-P1|P2}
[[ "$BLOCKING_THREAD_BADGES" =~ ^[A-Za-z0-9|]*$ ]] || { echo "::error::BLOCKING_THREAD_BADGES must be letters, digits and | only."; exit 1; }
BLOCKING_THREAD_AUTHORS=${BLOCKING_THREAD_AUTHORS-chatgpt-codex-connector}
[[ "$BLOCKING_THREAD_AUTHORS" =~ ^[A-Za-z0-9|-]*$ ]] || { echo "::error::BLOCKING_THREAD_AUTHORS must be letters, digits, - and | only."; exit 1; }
OPEN_BADGED_THREADS='.data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved | not)
  | select(("'"$BLOCKING_THREAD_AUTHORS"'" == "") or ((.comments.nodes[0].author.login // "" | sub("\\[bot\\]$"; "")) | test("^('"$BLOCKING_THREAD_AUTHORS"')$")))
  | select((.comments.nodes[0].body // "") | test("!\\[('"$BLOCKING_THREAD_BADGES"') Badge\\]")) | "open"'
REQUIRE_CODEX_REVIEW=${REQUIRE_CODEX_REVIEW:-false}
CODEX='chatgpt-codex-connector[bot]'
AUTOMERGE_DEPENDABOT=${AUTOMERGE_DEPENDABOT:-true}
AFTER_MERGE_WORKFLOWS=${AFTER_MERGE_WORKFLOWS-docker.yml security.yml}

lines() { sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d' <<< "$1"; }
mapfile -t required_workflows < <(lines "$required_workflows_text")
mapfile -t required_statuses < <(lines "$required_statuses_text")

dry_run() { [ "${DRY_RUN:-}" = 1 ] || [ "${DRY_RUN:-}" = true ]; }
run() { if dry_run; then echo "(dry run) $*"; else "$@"; fi; }
quiet() { if dry_run; then run "$@"; else "$@" >/dev/null; fi; }

if [ -n "${PR:-}" ]; then
  SHA=$(gh api "repos/$REPO/pulls/$PR" --jq .head.sha)
fi
[[ "${SHA:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::No head commit to check (SHA=${SHA:-})."; exit 1; }

# The open pull request whose head is this commit (none: it was merged, closed, or pushed to since). The status is
# the commit's, so two pull requests sharing a head (a fork reusing another's commit) can't each be judged: neither is.
prs=$(gh api --paginate "repos/$REPO/pulls?state=open&base=main&per_page=100" \
        --jq ".[] | select(.head.sha == \"$SHA\") | [.number, .user.login, .head.repo.full_name, .draft] | @tsv")
if [ -z "$prs" ]; then echo "No open pull request into main at $SHA."; exit 0; fi
if [ "$(wc -l <<< "$prs")" -gt 1 ]; then
  echo "::error::More than one open pull request has $SHA as its head; not deciding for any of them."
  quiet gh api -X POST "repos/$REPO/statuses/$SHA" -f state=failure -f context="$CONTEXT" \
    -f description="More than one open pull request has this commit as its head" ${RUN_URL:+-f target_url="$RUN_URL"}
  exit 1
fi
IFS=$'\t' read -r number author head_repo draft <<< "$prs"
CODEX_REVIEWED_HEAD='.[] | select(.user.login == "'"$CODEX"'" and .commit_id == "'"$SHA"'") | "reviewed"'
CODEX_COMMENTED_HEAD='.[] | select(.user.login == "'"$CODEX"'") | (.body // "") | (
  (capture("<!-- codex-review-state:v1 (?<s>[^\n]*) -->") | .s | fromjson? | .code_review?
     | select(.status == "completed" and .commit_sha == "'"$SHA"'") | "reviewed"),
  (capture("Reviewed commit:\\*\\* `(?<c>[0-9a-f]{7,40})`") | .c | select(. as $c | "'"$SHA"'" | startswith($c)) | "reviewed"))'
echo "Pull request #$number by $author at $SHA"
# CodeQL's default setup doesn't analyse Dependabot's pull requests (its check says the configurations "were not found"),
# so none of its runs ever comes: waiting for one would keep the gate pending, and the merge below, forever. A run that
# does come still counts, as for any workflow; the tests and the security scans are still required. Nor do the reviews
# run on them (no agent wrote them), so their statuses aren't asked for either.
if [ "$author" = "dependabot[bot]" ]; then
  required=(); for w in "${required_workflows[@]}"; do [ "$w" = CodeQL ] || required+=("$w"); done
  required_workflows=("${required[@]}")
  required_statuses=()
  REQUIRE_CODEX_REVIEW=false
fi

list() { local out="" x; for x in "$@"; do out+="${out:+, }$x"; done; echo "$out"; }

evaluate() {
  failing=() pending=() seen=()

  # 1. The workflows the pull request started: the newest run of each (a re-run or a reopen starts another).
  runs=$(gh api --paginate "repos/$REPO/actions/runs?head_sha=$SHA&per_page=100" --jq '.workflow_runs[]
           | select(.event == "pull_request" or (.event == "dynamic" and ((.path // "") | startswith("dynamic/github-code-scanning/"))))
           | [.created_at, .workflow_id, (if .event == "dynamic" then "CodeQL" else .name end), .status, (.conclusion // "")] | @tsv')
  latest=$(sort <<< "$runs" | awk -F'\t' 'NF { last[$2] = $0 } END { for (k in last) print last[k] }')
  while IFS=$'\t' read -r _ _ name status conclusion; do
    [ -n "${name:-}" ] || continue
    [[ "$name" =~ $IGNORED_WORKFLOWS ]] && continue
    seen+=("$name")
    if [ "$status" != completed ]; then pending+=("$name")
    elif ! [[ "$conclusion" =~ $OK_CONCLUSIONS ]]; then failing+=("$name ($conclusion)"); fi
  done <<< "$latest"
  for w in "${required_workflows[@]}"; do
    printf '%s\n' "${seen[@]:-}" | grep -qxF "$w" || pending+=("$w (not started)")
  done

  # 2. Other apps' check runs.
  checks=$(gh api --paginate "repos/$REPO/commits/$SHA/check-runs?per_page=100" \
             --jq '.check_runs[] | [.app.slug, .name, .status, (.conclusion // "")] | @tsv')
  while IFS=$'\t' read -r app name status conclusion; do
    [ -n "${app:-}" ] || continue
    [[ "$app" =~ $IGNORED_APPS ]] && continue
    if [ "$status" != completed ]; then pending+=("$name")
    elif ! [[ "$conclusion" =~ $OK_CONCLUSIONS ]]; then failing+=("$name ($conclusion)"); fi
  done <<< "$checks"

  # 3. Commit statuses: the newest of each context (the API lists newest first).
  statuses=$(gh api --paginate "repos/$REPO/commits/$SHA/statuses?per_page=100" --jq '.[] | [.context, .state] | @tsv')
  newest=$(awk -F'\t' 'NF && !seen[$1]++' <<< "$statuses")
  for s in "${required_statuses[@]}"; do
    grep -qxF "$s" <<< "$(cut -f1 <<< "$newest")" || pending+=("$s (not started)")
  done
  while IFS=$'\t' read -r context result; do
    [ -n "${context:-}" ] || continue
    [[ "$context" =~ $IGNORED_STATUSES ]] && continue
    case "$result" in
      success) ;;
      pending) pending+=("$context") ;;
      *) failing+=("$context ($result)") ;;
    esac
  done <<< "$newest"

  # 4. Open review threads whose first comment carries a blocking badge (Codex's "![P1 Badge](…)"): a reviewer's blocking
  # finding stays open until someone resolves its thread. An outdated thread (its line changed) counts until it is
  # resolved too, since a push doesn't say the finding was dealt with. A query that fails leaves the gate pending, not a
  # script that dies without posting a status.
  if [ -n "$BLOCKING_THREAD_BADGES" ]; then
    if blocking=$(gh api graphql --paginate -f owner="${REPO%%/*}" -f name="${REPO#*/}" -F number="$number" -f query='
        query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
          repository(owner: $owner, name: $name) { pullRequest(number: $number) {
            reviewThreads(first: 100, after: $endCursor) {
              pageInfo { hasNextPage endCursor }
              nodes { isResolved comments(first: 1) { nodes { body author { login } } } } } } } }' \
        --jq "$OPEN_BADGED_THREADS" | wc -l); then
      [ "$blocking" -eq 0 ] || failing+=("$blocking open $BLOCKING_THREAD_BADGES review comment$([ "$blocking" -eq 1 ] || echo s)")
    else
      pending+=("review threads (couldn't be read)")
    fi
  fi

  # 5. With REQUIRE_CODEX_REVIEW, a finished Codex review of this very commit: a pull request review on it (Codex posts
  # one when it has findings), or Codex's own comment naming it, by the full hash in its review state line or by at
  # least 7 leading digits after "Reviewed commit:" (a review that found nothing is a comment, not a review). Only
  # Codex's login counts. A review of an older commit doesn't: the head waits for its own.
  if [ "$REQUIRE_CODEX_REVIEW" = true ]; then
    if codex_reviews=$(gh api --paginate "repos/$REPO/pulls/$number/reviews?per_page=100" --jq "$CODEX_REVIEWED_HEAD") &&
       codex_comments=$(gh api --paginate "repos/$REPO/issues/$number/comments?per_page=100" --jq "$CODEX_COMMENTED_HEAD"); then
      grep -q reviewed <<< "$codex_reviews$codex_comments" || pending+=("Codex review of this commit")
    else
      pending+=("Codex review (couldn't be read)")
    fi
  fi

  if [ ${#failing[@]} -gt 0 ]; then
    state=failure; description="Failed: $(list "${failing[@]}")"
  elif [ ${#pending[@]} -gt 0 ]; then
    state=pending; description="Waiting for: $(list "${pending[@]}")"
  else
    state=success; description="Every check passed"
  fi
}

# Only CodeQL still to come: look again shortly rather than wait for an event that won't come.
only_codeql() { local x; for x in "${pending[@]}"; do [[ "$x" =~ $POLLED ]] || return 1; done; }
posted=""
for (( attempt = 0; ; attempt++ )); do
  evaluate
  echo "$state: $description"
  if [ "$state:$description" != "$posted" ]; then
    quiet gh api -X POST "repos/$REPO/statuses/$SHA" -f state="$state" -f context="$CONTEXT" \
      -f description="${description:0:140}" ${RUN_URL:+-f target_url="$RUN_URL"}
    posted="$state:$description"
  fi
  [ "$state" = pending ] && [ ${#failing[@]} -eq 0 ] && only_codeql && [ "$attempt" -lt "$POLL_ATTEMPTS" ] || break
  sleep "$POLL_SECONDS"
done

# Dependabot's pull requests merge themselves once the gate passes (any update type, a bump of a fleet reference too:
# the gate is the tests), as long as every commit is Dependabot's own, so nobody else's change rides along.
[ "$state" = success ] && [ "$author" = "dependabot[bot]" ] && [ "$head_repo" = "$REPO" ] && [ "$draft" = false ] || exit 0
if [ "$AUTOMERGE_DEPENDABOT" != true ]; then echo "Not merging #$number: AUTOMERGE_DEPENDABOT is off."; exit 0; fi
others=$(gh api --paginate "repos/$REPO/pulls/$number/commits" \
           --jq '.[] | select(.author.login != "dependabot[bot]" or .commit.verification.verified != true) | .sha')
if [ -n "$others" ]; then echo "::notice::#$number has commits from someone other than Dependabot; leaving it for review."; exit 0; fi
# --match-head-commit refuses if the pull request moved on since the checked commit.
run gh pr merge "$number" --repo "$REPO" --squash --match-head-commit "$SHA"
# A merge made with this token starts no workflows by itself: start main's release build and security scan.
for workflow in $AFTER_MERGE_WORKFLOWS; do
  run gh workflow run "$workflow" --repo "$REPO" --ref main
done
