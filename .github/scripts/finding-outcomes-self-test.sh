#!/usr/bin/env bash
# The self-test of the finding outcomes (FLEET-FINDING-OUTCOMES) with the stub reviewer (tests/fakes/claude), the same
# steps agent-review.yml runs and no secret: a blocking finding is recorded as open; a push the stub then passes
# resolves it; the owner's `/fleet dismiss` shows the dismissal and leaves every recorded status as it was, and a
# non-owner's is ignored. Run from fleet's checkout (self-test.yml and tests/test_review_outcomes.py).
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

export CLAUDE="$root/tests/fakes/claude" MODEL=sonnet BUDGET=1 MAX_TURNS=1
export ANTHROPIC_API_KEY=not-a-key-the-stub-reviewer-reads-none RUN_URL=https://example.com/run
first=$(printf 'a%.0s' {1..40})
second=$(printf 'b%.0s' {1..40})

# review ANSWER SHA COMMENTS.jsonl OUT.md: the stub's answer through the runner script, the verdict and the outcomes.
review() {
  local dir
  dir="$work/$(basename "$4" .md)"
  mkdir -p "$dir"
  cp "$root/.github/agent-review/prompt.md" "$root/.github/agent-review/schema.json" "$dir/"
  (cd "$dir" && FAKE_REVIEW=$1 "$root/.github/scripts/agent-review-run.sh" > /dev/null)
  GITHUB_OUTPUT="$dir/out" REVIEWED_SHA=$2 python3 -I "$root/fleet_tools/agent_review.py" report "$dir/output.json" > /dev/null
  sed -n 's/^comment=//p' "$dir/out" | base64 -d > "$4"
  sed -n 's/^findings=//p' "$dir/out" | base64 -d > "$dir/findings.json"
  python3 -I "$root/fleet_tools/review_outcomes.py" --finish "$4" --repo example/app --pr 1 --sha "$2" --comments "$3" --findings "$dir/findings.json"
}

# bot_comment FILE.md: the comment as the comments API would list it, one JSON line.
bot_comment() {
  python3 -I -c 'import json, sys; print(json.dumps({"user": "github-actions[bot]", "association": "NONE", "url": "", "body": open(sys.argv[1], encoding="utf-8").read()}))' "$1"
}

outcomes() {
  python3 -I "$root/fleet_tools/review_outcomes.py" --repo example/app --pr 1 --comments "$1"
}

fail() { echo "::error::$*"; exit 1; }

: > "$work/none.jsonl"
review blocking "$first" "$work/none.jsonl" "$work/one.md"
bot_comment "$work/one.md" > "$work/after-one.jsonl"
id=$(grep -o 'f:[0-9a-f]\{8\}' "$work/one.md" | head -n 1 | cut -c 3-)
[ -n "$id" ] || fail "The blocking finding has no id in the comment"
outcomes "$work/after-one.jsonl" | grep -q "^f:$id  open " || fail "The blocking finding isn't recorded as open"
echo "f:$id open after the blocking review"

review pass "$second" "$work/after-one.jsonl" "$work/two.md"
bot_comment "$work/two.md" > "$work/after-two.jsonl"
outcomes "$work/after-two.jsonl" | grep -q "^f:$id  resolved " || fail "The fixed finding isn't recorded as resolved"
grep -q "resolved at \`${second:0:7}\`" "$work/two.md" || fail "The comment doesn't say the finding was resolved"
echo "f:$id resolved after the passing review"

{
  cat "$work/after-two.jsonl"
  printf '{"user": "someone", "association": "MEMBER", "url": "https://github.com/example/app/pull/1#issuecomment-2", "body": "/fleet dismiss f:%s a member cannot dismiss it"}\n' "$id"
  printf '{"user": "owner", "association": "OWNER", "url": "https://github.com/example/app/pull/1#issuecomment-3", "body": "/fleet dismiss f:%s the stub blocks on purpose\\nmore text that is not part of the reason"}\n' "$id"
} > "$work/dismissed.jsonl"
review pass "$second" "$work/dismissed.jsonl" "$work/three.md"
[ "$(sed -n 4p "$work/two.md")" = "$(sed -n 4p "$work/three.md")" ] || fail "A dismissal changed the recorded statuses"
grep -q 'dismissed by the owner, "the stub blocks on purpose"' "$work/three.md" || fail "The comment doesn't show the dismissal"
grep -q "a member cannot dismiss it" "$work/three.md" && fail "A member's dismissal was shown"
outcomes "$work/dismissed.jsonl" | grep -q "^f:$id  dismissed .*(the stub blocks on purpose)$" || fail "review-outcomes doesn't show the dismissal"
echo "f:$id dismissed by the owner; the record is unchanged"
