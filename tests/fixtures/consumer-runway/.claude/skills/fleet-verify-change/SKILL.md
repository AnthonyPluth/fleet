---
# vendored from fleet v0.1.0 (plugins/fleet/skills/verify-change/SKILL.md) by `fleet sync`; edit it in fleet, not here: `fleet drift` fails on any change
name: fleet-verify-change
description: Use before marking a pull request ready for review, in a repository whose pyproject.toml sets [tool.fleet.verification]. Lists the change's dimensions (each user-visible behaviour, failure path, migration, permission and invariant), verifies each with the app's own tools and verify skills, and writes the Verification block into the pull request's description - every dimension verified with evidence from this session, or not verified with the reason.
---

# Verify the change and record it

The pull request's description says, one dimension at a time, what was verified, how, and on what evidence, and names what couldn't be verified and why. "Testing" says what was run (`make check`, specific tests); the Verification block says, per behaviour the change touches, whether it was checked. Reviews read it, `fleet checks` parses it, and the monthly report counts the gaps that keep coming back, so the block is a record, not a claim.

## When this applies

Before the pull request is marked ready for review, in a repository whose `pyproject.toml` has a `[tool.fleet.verification]` table. While the table has `required = false`, a missing or malformed block is a warning on the pull request; with `required = true` it fails `fleet checks` on a ready pull request. Write the block either way.

## List the dimensions

Read the diff (`git diff origin/main...HEAD`) and the issue, and write down every dimension the change touches:

- each behaviour a user sees: a page, a flow, a command, an API response, a message;
- each failure path: every new error, rejection, early return or timeout, and what the user sees then;
- each migration, and the data it moves;
- each permission, token or grant, and when it ends;
- anything `AGENTS.md` or `.github/agent-review/invariants.md` calls an invariant that the change comes near.

A change with one dimension has one row. A refactor still has the behaviours it must not have changed.

## Verify each one

Use the repository's own tools, in this order of preference:

1. The app's verify skill (`.claude/skills/verify-*/`), which says how to run the app and drive its flows, and `make verify` or the control smoke where the repository has one: for anything on screen or in a flow.
2. A test that fails before the change and passes after, run in this session: for logic, failure paths and migrations.
3. A manual step you ran in this session, with its command and output.

Never mark a dimension `verified` on reasoning, on a test you didn't run, or on a run from another session. If the session lacks what a dimension needs (a fake, a flow, a fixture, a credential, a device), the dimension is `not verified`, and its How says what is missing and what would make it verifiable.

## Write the block

In the description's "Verification" section (add the heading after "Testing" if the template lacks it), between the two marker lines, each alone on its line:

```md
## Verification

<!-- fleet-verification -->
| Dimension | Status | How | Evidence |
| --- | --- | --- | --- |
| Settings page shows the new toggle | verified | `make verify`, flow `settings-toggle` | `screenshots/flow-settings-toggle-desktop.png` |
| Toggle rejected without the permission | verified | `tests/test_settings.py` | `test_a_viewer_cannot_change_the_toggle` passed |
| Sync against a live account | not verified | no fake for the provider; a fake under `tests/fakes/` would make it testable | - |
<!-- /fleet-verification -->
```

The rules the parser holds:

- the header row is exactly `| Dimension | Status | How | Evidence |`, and there is at least one row;
- Status is exactly `verified` or `not verified`;
- a `verified` row has a non-empty How (the command, flow, test or manual step) and non-empty Evidence (a test name, a flow name, a screenshot path, a command's output summary); `-` and `—` count as empty;
- a `not verified` row's How says why it couldn't be verified and what would make it verifiable;
- the markers and every row start at the first column (the leading pipe may be left off); a pipe inside a cell is written `\|`; between the markers, every line with an unescaped `|` is read as a row, so a note there escapes its pipes or stays outside;
- one block per description; lines between the markers without a pipe are ignored;
- the only code fence the parser knows is a plain top-level one (backticks or tildes at the start of the line); an indented, listed or quoted fence, an indented or prefixed marker or row, a tab on a fence line or a multiline HTML comment is rejected as ambiguous. That rule reaches the whole description, not only the record: write every fence at the first column and outside lists, anywhere in the description, command output in "Testing" included; with `required = false` a rejection is only a warning.

Every gap goes in as `not verified` with its reason, never left out: a reviewer who sees the gap can judge it, and one who doesn't can't. Keep "Testing" for what was run. `fleet verification show --pr N` prints the block as the tools read it.

## Before marking ready

Run `fleet checks --pr-event <payload>` locally where you can, or read the "fleet checks" run on the pull request: a warning or error there names the line and the rule. Fix the description, which re-runs the check, rather than arguing with it in a comment.
