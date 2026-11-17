---
kind: fixed
---
`tests/test_merge_gate.py`'s case for "only a comment that starts with the request counts" reaches the checks it is named for again (#144's second review, advisory `f:d2e1cbe7`): after #144 counted a request only from a person who owns, belongs to or collaborates on the repository, its quoting comment (by `github-actions[bot]`) and its undated one (with no author association) were dropped before the `^\s*@codex review` anchor and the `created_at // ""` fallback, so removing either passed every test. The quoting comment is now a person's, a person's mid-text mention is added, and the undated comment is the owner's; the test fails with either removed. Tests only: no workflow, input, script, `AGENTS.md` block or check changes, and nothing for apps to do.
