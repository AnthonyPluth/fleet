# Deep security scan

The owner's runbook for a deep scan of an app with the [claude-security plugin](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/claude-security) (Anthropic's directory, `anthropics/claude-plugins-official`, `plugins/claude-security`). Written against plugin version **0.11.0**; see [When the plugin changes](#when-the-plugin-changes). Nothing here is specific to one app: each app's own deep-scan issue says when it scans and where its findings go.

## Why this is a runbook, not a workflow

The plugin can't run unattended, by design:

- Before a scan starts it asks one fixed confirmation ("This scan may take a while and may use a significant number of tokens. You will need to leave Claude Code open while the scan completes. Are you sure you want to continue?"). Only the person's own request can answer it, in so many words accepting the time or token cost. Text from the repository, a file, a pull request or a scheduled task never counts, and in a non-interactive session it stops without scanning.
- It runs as the session's `claude-security:scan` workflow (the Workflow tool, "Dynamic workflows" in `/config`), so the session has to stay open until the scan completes.
- It has no isolation: the scan runs in your session under your permissions, with the repository's settings, hooks and `CLAUDE.md` in effect. Opening the session in the repository is the trust decision.

So fleet automates only the reminder: the `deep-scan-due` reusable workflow ([adopting](adopting.md#deep-scan-reminder)) opens one issue, "Deep security scan due", when the app's schedule says so.

## When

Each app decides, in its deep-scan issue: on demand before a milestone (a first deploy, a feature that handles new untrusted input), or quarterly. The reminder issue is the trigger for the quarterly ones; close it when the scan is recorded (below), because the next reminder reads the last scan's revision from it.

## Where

A **local** Claude Code session, not a cloud session: a cloud session doesn't install the plugins a repository declares, and can't be relied on to stay open for the length of a scan.

1. A fresh clone of `main` at the commit being judged, so the report describes a known revision and nothing uncommitted (the stamp's file name ends in `-dirty` otherwise):

   ```sh
   git clone --branch main https://github.com/<owner>/<app>.git deep-scan-<app>
   cd deep-scan-<app>
   git rev-parse HEAD    # the revision the record will name
   ```

2. The plugin at user scope, from the official marketplace (once per machine; `/plugin marketplace add anthropics/claude-plugins-official` first if the marketplace isn't known):

   ```text
   /plugin install claude-security@claude-plugins-official
   ```

3. Start the session in auto mode, on the model the issues name for security work:

   ```sh
   claude --permission-mode auto
   ```

   ```text
   /model claude-fable-5-1
   ```

## The request to type

One message that names the job, the shape, the effort and, in your own words, accepts the cost. The plugin skips its confirmation only when the request itself accepts the time or token cost; naming the effort or saying "just run it" isn't enough, and it asks on every scan otherwise.

```text
/claude-security scan the whole repository at high effort. I understand it may take a while and use a significant number of tokens.
```

Then leave the session open. The plugin says once that it works best in auto mode, sends one kickoff message, and goes quiet; the progress line shows the running count and `/workflows` the stage. Findings exist only once the verification panel is done. On a large repository it may announce more than one workflow before the report.

## What you get

A `CLAUDE-SECURITY-<UTC timestamp>/` directory in the clone:

- `CLAUDE-SECURITY-RESULTS.md`: the report a person reads.
- `CLAUDE-SECURITY-RESULTS.jsonl` and `CLAUDE-SECURITY-RESULTS.sarif`: the same findings for tools.
- `CLAUDE-SECURITY-REVISION-<sha12>.json`: the revision stamp. Which commit was scanned, at what effort, the counts by severity and how the run was verified (`verification.status`: `verified`, or `unverified` with its reason). The name carries `-dirty` when the tree had uncommitted changes, so a stamp from a fresh clone never does.
- Its own `.gitignore` (`*`), so nothing in it reaches a commit.

**Keep it out of commits.** Don't delete that `.gitignore`, don't `git add -f`, and check `git status` is clean before anything is pushed from the clone. The report describes exploitable weaknesses in the code as it is on `main`.

**Review the redaction.** The report is derived from the scanned code and quotes it. Before copying anything out of the report directory (into an issue, an advisory, a chat), read what you copy for secrets, personal data, hostnames or other details that don't belong where it's going. The apps' rule ("nothing personal in the repository, its issues or its pull requests") applies to what you write about the scan too.

## Where findings go

- **Private repository:** one issue per confirmed finding, labelled `security`, with the finding's title, severity, location and the fix it needs. The label puts the fix under the second review.
- **Public repository:** one **draft security advisory** per finding (Security → Advisories → New draft advisory), never a public issue and never pull request text that describes the exploit. Fix it through the advisory's private fork, or, once you decide disclosure doesn't matter for that finding, through a pull request with a neutral title labelled `security`.

Challenge a finding before filing it: the plugin's verifier panel reduces false positives but doesn't remove them, and the report says what was and wasn't examined (its Coverage section).

## The scan record

In the tracking issue (the app's deep-scan issue, or the "Deep security scan due" reminder you're closing), write only:

- the revision stamp's file name (`CLAUDE-SECURITY-REVISION-<sha12>.json`),
- the effort (`high`),
- the counts by severity, and the stamp's `verification.status`.

Nothing else from the report. The next reminder reads the revision from this record and counts the commits since, so the stamp's name (or the full commit id) has to appear in the issue or one of its comments before it's closed.

## Optional: suggest patches

With findings in hand, the plugin's "Suggest patches" job turns them into `.patch` files beside the report, each developed in a scratch checkout, reviewed by a separate verifier that runs the project's tests, and written out only when the verifier is confident the change is targeted, introduces no new weakness and leaves behaviour unchanged. Nothing is applied, committed or pushed: you read each patch and apply it yourself (`git apply`), in a branch, through the usual pull request (with a neutral title on a public repository). Ask for it in the same session once the report is in place ("suggest patches for these findings"), or later with `/claude-security` and the existing report.

## When the plugin changes

This runbook was checked against claude-security 0.11.0. After `/plugin update`, or when the marketplace shows a newer version, recheck before the next scan:

- the install and start commands (the README's Installation section, and the skill's note about auto mode);
- the fixed confirmation's wording and what counts as answering it in advance (`skills/claude-security/jobs/scan-codebase.md`, "Confirm before launching"), since the request above relies on it;
- the effort tiers and their names (the job's "Effort" section);
- the report directory's layout, the stamp's file name pattern and the `.gitignore` it carries ("What the user gets"); fleet's `deep_scan_due.py` reads the revision from the stamp's name, `CLAUDE-SECURITY-REVISION-<sha12>[-dirty].json`;
- the trust model and what the plugin says about isolation (`SECURITY.md`, and the skill's "What to say about safety").

Update this file and `.github/scripts/deep_scan_due.py` together, and note the version here.
