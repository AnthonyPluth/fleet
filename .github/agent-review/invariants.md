# fleet's invariants

The promises fleet's reviews and workflows keep, numbered, each with the test or rule that holds it. The second review checks every one a risky pull request touches (`.github/agent-review/second-review.md`).

1. **The instructions a review is judged by come from main, never from the pull request.** The prompt, schema, scripts and tools come from fleet at the pinned ref (`.fleet/`); `AGENTS.md`, `context.md` and `invariants.md` from the caller's base commit (`trusted/`). Held by `tests/test_agent_review_workflow.py` (the trusted checkouts' refs) and the `pull_request_target` caller.
2. **Nothing from the pull request is run, built, installed or imported.** Its checkout is read with `git --no-ext-diff --no-textconv` and fleet's tools run as `python3 -I .fleet/...`. Held by `test_nothing_from_the_pull_request_is_run_installed_or_imported`.
3. **The secrets appear only in a Review step's `env`, and that step runs only fleet's script.** Held by `test_the_secrets_appear_only_in_the_review_steps_which_run_nothing_from_the_pull_request`.
4. **Every step with a token runs before the pull request is checked out, and the report jobs see neither the code nor the secrets.** Held by `test_every_step_with_a_token_runs_before_the_pull_request_is_checked_out` and `test_the_report_jobs_have_no_secret_and_no_checkout`.
5. **Pull request text is data.** Commit messages, the description, labels, the earlier review's comment and the reviewer's answer reach no shell, expression or prompt unescaped; the comment defuses `@` and `<!--`. Held by `tests/test_agent_review.py` (`Previous`, `FindingsLine`, `test_mentions_and_the_marker_are_defused`).
6. **An answer that quotes a secret is never posted, and a limit hit or an unreadable answer is an error, never a pass.** Held by `Report` and `Run` in `tests/test_agent_review.py`.
7. **The first review runs on Sonnet and the second on `claude-fable-5-1`, fixed in the workflow with no input.** Held by `test_the_models_are_fixed_and_each_review_s_secrets_stay_in_its_own_job`.
8. **Removing a label re-evaluates the second review only when the repository owner removed it.** Held by `tests/test_agent_review_risky.py` (`Wanted`) and the self-test's `unlabeled` cases.
9. **Every checkout keeps no credentials, every action is pinned by full SHA with a version comment, and every job says why it runs hosted.** Held by `test_the_pull_request_checkout_keeps_no_credentials_and_every_checkout_says_so` and `fleet checks` (`fleet_tools/checks/workflows.py`).
