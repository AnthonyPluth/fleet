# Releasing fleet

Every merge to `main` is a release. `.github/workflows/release.yml` tags the merged commit `vX.Y.Z` and publishes its GitHub Release, with no release pull request and no release commit: nothing in fleet's tree names fleet's version, so the commit that merged is the release as it is.

The apps call fleet by pinned reference: each caller pins a reusable workflow by commit SHA with the tag in a comment (`@<sha>   # vX.Y.Z`), Dependabot bumps those pins from the tags, and the reusable workflows fetch fleet's own scripts, prompts and tools at their own commit (`job.workflow_sha`, the commit the pin names). fleet's tools take their identity from their commit too (`fleet_tools/identity.py`): `git rev-parse HEAD` in a checkout, `vcs_info.commit_id` in `direct_url.json` (PEP 610) for an install from git, such as `tools/fleet`'s `uvx --from "git+https://github.com/AnthonyPluth/fleet@<commit>"`. `fleet --version` prints that commit, with the tag when `git describe --exact-match` finds one. `fleet sync --to vX.Y.Z` refuses unless the commit that version names (the consumer's workflow pins at it, `--commit`, or the tag's from `git ls-remote`) is the running one; `vX.Y.Z` itself is only the label it writes into the consumer's markers, vendored notes, marketplace `ref` and `.fleet/version`. `fleet drift` reads content whose `.fleet/version` names the running commit from the running fleet's files, and any other version from its tag in fleet's history. None of that reads a version from fleet's tree.

## What a merge releases

The version is the next one after the newest tag, from the merged pull request's title (the merge commit's subject; with several merges since the last tag, the largest step among their titles):

- **Patch** by default: `fix: …`, `docs: …`, `chore: …`, a Dependabot bump, anything else. A merge that only changes documentation or tests releases a patch too.
- **Minor** for `feat: …` (or `feat(scope): …`).
- **Major** for a breaking change: `feat!: …` (a `!` before the title's colon), and, whatever the titles say, a `kind: breaking` changelog fragment added since the last tag ("when in doubt, take the larger"). Release check holds the title to the fragment before the merge: breaking (`feat!: …`, or a `BREAKING CHANGE:` line in the description) exactly when the fragment is `breaking` ([Changelog fragments](#changelog-fragments)).

`fleet next-version --first-parent --fragments` works this out: the apps' planner, which they keep unchanged, reading only `main`'s first-parent commits since the last tag, one per merged pull request, never the commits on its branch, and making the release a major when a `breaking` fragment was added since that tag's tree. fleet goes squash-only (an owner setting); a merge commit titled with the pull request's title, as fleet's merges are while merge commits are allowed, reads the same with `--first-parent`, and a rebase merge would not, so it stays off.

**Breaking pull requests merge one at a time.** Each breaking change is a major release of its own, so that each app gets one "fleet vN needs a migration" issue per breaking change. A release run covers every merge since the last tag (below), so two breaking pull requests merged before the first one's run has tagged ship as one major, with both entries in its notes. fleet has no merge gate to hold the second back: merge a breaking pull request only once the previous merge's release is out (its Release run has finished and the tag is on [fleet's releases](https://github.com/AnthonyPluth/fleet/releases)).

## The release workflow

`release.yml` runs on every push to `main`, from `main`'s own files, in three jobs:

1. **make check** (`contents: read`): the whole suite on the merged commit, exactly as fleet CI runs it, because two pull requests that each passed can break `main` together. Nothing is released while it fails; the next merge that fixes it releases everything since the last tag.
2. **Plan** (`contents: read`), on a fresh checkout and the runner's own `/usr/bin/python3 -E -s`: `fleet next-version --first-parent --fragments --sha <the commit>` gives the version, then `fleet release notes X.Y.Z --since <the previous tag> --at <the commit>` the notes ([below](#changelog-fragments)). The previous tag is the newest release tag in `main`'s first-parent history before the commit (`git describe --first-parent` from its first parent). A commit that isn't `main`'s newest any more, and isn't tagged yet, is skipped (`skip`): the newest commit's run releases it. The notes reach the next job as one base64 line.
3. **Tag and release** (`contents: write`, nothing else): no checkout and no file of the repository, only `gh`. It checks the plan's version is `vX.Y.Z` and its notes one base64 line, tags the commit the push names (`github.sha`) through the API, and publishes the GitHub Release titled `vX.Y.Z` with those notes and `--verify-tag`.

It can be re-run. A tag already on this commit (a run whose publish failed) passes without tagging again, a release that already exists isn't published again, and a re-run of the plan on a tagged commit plans that tag again, with the same notes. A tag that exists on another commit fails the run: a release tag is never moved or reused. The repository's tag ruleset ("release tags: no update or deletion") refuses moving or deleting a `v*` tag, and immutable releases keep a published release as it was; neither restricts creating a tag, which the workflow's own token does.

**Rapid merges.** One concurrency group, `fleet-release`, with `cancel-in-progress: false`: two runs never overlap, so two never plan the same version. GitHub keeps one pending run per group, so a merge while one run works and another waits replaces the waiting one. With A running, B waiting and C merged, B's run is cancelled and C's waits; A's plan skips if C has landed by then, and C's release covers A, B and C, whose notes list all three.

## Changelog fragments

Each pull request adds its changelog entry as a file of its own, a fragment in `changelog.d/`, rather than a line in a shared section, so pull requests open at the same time never conflict over it. `CHANGELOG.md` is frozen at 4.0.0: its sections up to there stay as they are, and nothing is added to it. The releases 4.1.0 and 5.0.0 folded their fragments into their notes, `changelog.d/released/X.Y.Z.md`; that folder is frozen at 5.0.0, the last release that had a release pull request, and every later release's notes are its fragments.

```md
---
kind: added
---
`fleet x` (#123): what changed, for whom, and what an app has to do about it, on one line.
```

- **The name**, `changelog.d/<name>.md`, is lowercase letters, digits and hyphens: name it after the branch or the change (`merge-gate-auto-ready.md`), so two open pull requests don't add the same file, and so it takes no merged fragment's name. One pull request adds one fragment.
- **The kind** is `breaking`, `added`, `changed` or `fixed`. `breaking` is for a change a consumer has to act on ([below](#what-a-version-number-promises)); it is listed under Changed, starting with **Breaking:**. The other three are Keep a Changelog's Added, Changed and Fixed.
- **The entry** is one line (don't wrap it), written as `CHANGELOG.md`'s are, without the `- ` or the **Breaking:** that the notes add.

Fragments are never folded or deleted: a release's notes are the fragments its tree adds to the previous tag's (`git diff --diff-filter=A` between the two tags' trees, with `changelog.d/released/` left out), grouped under Added, Changed (the `breaking` ones first) and Fixed, each group in the order of the fragments' names, then under Changed the subject of each first-parent commit since the previous tag that added none (a Dependabot bump). `fleet release notes X.Y.Z --since vW [--at REF]` prints them (`REF` defaults to `HEAD`); it fails when there is nothing to release, on a fragment that doesn't parse and on a ref that isn't one. `fleet release notes X.Y.Z` without `--since` prints a folded release's notes, from `changelog.d/released/` up to 5.0.0 or from `CHANGELOG.md` up to 4.0.0.

`fleet sync-plan` lists in an app's sync pull request or migration issue every release between the app's content and its pins, newest first, wherever its notes are: first the fragments the pins' tag adds to the content's tag, under "Releases after vA, up to vB", then the folded notes and `CHANGELOG.md`'s sections. So a sync from 5.0.0 or earlier to a later release lists the new fragments, the 5.0.0 and 4.1.0 notes and the old sections together. `sync.yml` fetches the content's tag beside the pins' (both one commit deep); when it can't, or the clone isn't one, the body says the fragments couldn't be read and points at fleet's releases, and the sync goes on.

`fleet self-check` (in `make check`, and so in "fleet CI" and the release's first job) fails on a fragment that doesn't parse, on anything else in `changelog.d/` but `released/`, on notes in `changelog.d/released/` after 5.0.0, and on a `## [Unreleased]` section, or one newer than 4.0.0, in `CHANGELOG.md`. On every pull request but Dependabot's, "Release check" (`.github/workflows/release-check.yml`, on the runner's own `python3`) then runs `fleet release check --base HEAD^1` with the pull request's title and description (HEAD is the pull request merged into its base, HEAD^1 that base), and again whenever the pull request is edited, so a new title is checked too. It fails a pull request that adds no fragment, changes or deletes a merged one (a release's notes are read from the tags' trees, so a merged fragment is part of a released version's notes; a correction is a fragment of its own), touches `changelog.d/released/` or adds an entry to `CHANGELOG.md` (correcting one is fine), and any fragment that doesn't parse. Run it on your committed branch before pushing: `poetry run fleet release check --base origin/main --title "feat: …"`.

The title and the fragment must agree on the kind of change. A `breaking` fragment needs a title that says so (`feat!: …`, a `!` before the colon, or a `BREAKING CHANGE:` line in the description), and a title or description that says so needs a `breaking` fragment, so a breaking change can't go out as a minor or a patch, nor a major without the entry that tells an app what to change. An `added` fragment needs a `feat: …` title (`feat(scope): …` too), or a breaking one, which then needs its `breaking` fragment as well, so an addition can't go out as a patch. That one is the title's alone: a `feat:` line in the description doesn't count, because the merge commit's subject is the title and fleet's merges leave its body empty. A `#major` or `#minor` at the end of the title is refused: GitHub adds ` (#<number>)` after the title in the merge commit's subject, so it would never reach `main`. The check prints the kind of change the title makes it (major, minor or patch), which is the release its merge makes (with a `breaking` fragment it is a major in any case).

## Nothing in the tree names the version

There is no `VERSION` file and no `FLEET_REF` stamp, and `fleet self-check` fails if one comes back:

| Where | What it holds | `fleet self-check` fails on |
| --- | --- | --- |
| `VERSION` | nothing: the file is gone | the file existing |
| the workflows | each reusable workflow's fleet checkout at `inputs.fleet-ref \|\| job.workflow_sha` ([AGENTS.md](../AGENTS.md), the fleet-ref convention) | a `FLEET_REF` key in any workflow, or a workflow reading `FLEET_REF` |
| `plugins/fleet/.claude-plugin/plugin.json` | the constant `"version": "0.0.0"`, never a release (an owner decision: anyone who installs the plugin from the marketplace stays on one cached copy; the apps vendor it with `fleet sync` and don't enable it) | any other version |
| `pyproject.toml` | the constant `version = "0.0.0"` (Poetry needs one; nothing reads it) | any other version |
| the snippets in `docs/` (fenced blocks: a `uses: AnthonyPluth/fleet/…@<commit>   # vX.Y.Z` pin comment, `"ref": "vX.Y.Z"`, `fleet sync --to vX.Y.Z`, a consumer's `fleet:begin <block> vX.Y.Z` marker) | the placeholder `vX.Y.Z`, and `<commit>` (or a full commit) where a snippet fetches fleet | any other version, or a ref that can move |
| fleet's own `AGENTS.md` | the `fleet:begin <block>` markers with no version, and each block byte for byte its `agents-md/sections/` file (`poetry run fleet sync --self` writes them) | a version in a marker, a block that differs, a missing block or one fleet doesn't ship |
| `tests/fixtures/` | the fixture consumers' content (`fleet sync --self`), pin comments, markers and `.fleet/version`, and the drifted fixture's, all at `v5.0.0`, the last stamped release, for good, under the made-up commit `FIXTURE_COMMIT` (`fleet_tools/release.py`) | (their own tests hold them there) |

A snippet's value is read whole after its prefix, so a malformed one (`fleet sync --to v1.0`, `# v1.0`) fails rather than going unseen. Prose outside the fenced blocks is not checked: it may name versions on purpose. The fixtures' content is this tree's under the made-up commit, so the tests and the self-test tell `fleet drift` to take it for the running fleet's (`--own-commit`, and `checks.yml`'s self-test-only input `fixture-commit`).

## A bad release

A bad release is reverted, and the revert's merge is the next patch: a release tag is never moved or deleted, and there is no "withdrawn" release. Revert the pull request (`git revert -m 1 <its merge commit>`, or GitHub's Revert button), put its fragment back (it is part of the bad release's notes, and Release check refuses a pull request that deletes a merged fragment: `git checkout <its merge commit> -- changelog.d/<its name>.md`), and add the revert's own fragment, `kind: fixed`, saying what it takes back and from which release. A title like GitHub's `Revert "…"` or `fix: revert …` makes it a patch.

The apps take a release 3 days after it is out at the earliest (their Dependabot cooldown), and their own checks run the new reusable workflows on the bump pull request. A known gap, accepted: when the fix for a bad vN is less than 3 days old, Dependabot can still offer vN, and an app's merge gate merges that bump if the app's checks pass.

## What a version number promises

fleet follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html), judged from the consumer's side: what an app has to change when its pins move.

- **Major** (`2.0.0`): a consumer must change its caller or its `AGENTS.md`. An input removed or renamed, an input whose default changes what an existing setup does, a secret or permission a caller must now pass, a status or check renamed (a ruleset or merge gate names it), an `AGENTS.md` block removed or renamed, a `[tool.fleet]` key removed or its meaning changed. `sync.yml` doesn't sync a newer major by itself: it opens "fleet vN needs a migration: see CHANGELOG", so the changelog entry says what to change.
- **Minor** (`1.1.0`): new things that change nothing until a consumer asks for them, or that a consumer takes without editing anything: a new input with a default that keeps the old behaviour, a new reusable workflow, a new check (opt-in by its key, or on by default when it can't fail an app that followed the docs), a new skill, a new `AGENTS.md` block, and any change to a review's prompt or model. A prompt or model change is always called out in its changelog entry, because it changes what the reviews say without a line of the app changing.
- **Patch** (`1.0.1`): fixes that keep every documented behaviour: a bug, a message, a doc, a dependency bump.

When in doubt between two, take the larger one. Each changelog entry that needs a consumer to act is a `breaking` fragment, which the notes start with **Breaking:** (before 1.0.0 these read **Breaking (pre-1.0)**), and one of them makes the release a major whatever its title says.

## After a release

Dependabot opens the apps' pin bumps (the `fleet` group, on the apps' first run once the release is past its 3-day cooldown). The owner merges each bump, or the app's merge gate does once the checks pass where the app sets `automerge-fleet-bumps: true`, and the app's `sync.yml` follows the merged bump with the content's pull request, or a migration issue for a new major. The released commit can't name its own SHA, so nothing in fleet stamps it: the apps' `fleet sync` takes the commit from their pins (Dependabot resolved the tag to it) and writes it into `.fleet/version`, which `tools/fleet` runs fleet at.

## The threat model

`release.yml` is in the second review's always-risky paths (`agent-review-caller.yml`), with what it runs: everything under `fleet_tools/` (the Plan job runs `fleet_tools/cli.py`, which imports every module, so a change to any of them can change the version or the notes it hands on), and Release check's `release-check.yml`.

- **What it trusts.** It runs only on a push to this repository's `main`, from `main`'s own files: no pull request's content, event or title reaches it except as the merged commit itself, which is as trusted as `main`. The suite and the planner run that commit's code with a read-only token and no secret, each on a runner of its own, so nothing the suite installs (Poetry's environment, the locked Claude Code, actionlint) can touch the planner's checkout.
- **The write token.** `contents: write`, and nothing else, is held by one job that checks nothing out and runs no file of the repository: only `gh`, with every value through `env` (never an expression in a script), the commit from the push event, and the plan's version and notes checked before use (`vX.Y.Z`, one base64 line). Code in a merged commit can change the version and the notes the plan hands on, and so which new tag the commit gets, but not what the write job runs or which commit it tags: it can't push a branch, move or delete a tag, or reach anything else the token could.
- **Before the merge.** fleet CI, the Self-test, Release check (title and fragment), and the second review on the always-risky paths. A pull request could weaken the checks it is checked by, as with any CI check; the floor is the planner, which runs from `main`'s files after the merge, and makes a breaking fragment a major whatever the title says.
- **Tags.** A tag once created never moves (the ruleset) and a release never changes (immutable releases). Creating a `v*` tag isn't restricted (an owner decision, since the release's own token creates them): anyone who can push to the repository, or a workflow with a write token, can create one, as before. The planner skips a version that is taken.
- **After.** The apps' cooldown and their own checks on the bump pull request ([A bad release](#a-bad-release)).
