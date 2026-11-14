# Releasing fleet

The apps call fleet by pinned reference: each caller pins a reusable workflow by commit SHA with the tag in a comment (`@<sha>   # vX.Y.Z`), Dependabot bumps those pins, and the reusable workflows fetch fleet's own scripts, prompts and tools at their own commit (`job.workflow_sha`, the commit the pin names). A release is one commit where every version below agrees, tagged once and never moved. `FLEET_REF` is still among those versions, though nothing reads it any more; the stamps go when fleet releases on every merge (#112).

fleet's tools don't take their identity from these stamps either: which fleet is running is its commit (`fleet_tools/identity.py`), `git rev-parse HEAD` in a checkout and `vcs_info.commit_id` in `direct_url.json` (PEP 610) for an install from git, such as `tools/fleet`'s `uvx --from "git+https://github.com/AnthonyPluth/fleet@<commit>"`. `fleet --version` prints that commit, with the tag when `git describe --exact-match` finds one. `fleet sync --to vX.Y.Z` refuses unless the commit that version names (the consumer's workflow pins at it, `--commit`, or the tag's from `git ls-remote`) is the running one; `vX.Y.Z` itself is only the label it writes into the markers, the vendored notes, the marketplace `ref` and `.fleet/version`. `fleet drift` reads content whose `.fleet/version` names the running commit from the running fleet's files, and any other version from its tag in fleet's history. `VERSION` is read by `release.yml`, `fleet release prepare` and `fleet self-check`'s stamp checks, and by nothing else.

## What a release stamps

| Where | What | Before v1.0.0 |
| --- | --- | --- |
| `VERSION` | `X.Y.Z` | `0.x` |
| `changelog.d/` | the fragments, `changelog.d/<name>.md`, folded into the release's notes, `changelog.d/released/X.Y.Z.md` (`## [X.Y.Z] - YYYY-MM-DD`), and deleted ([Changelog fragments](#changelog-fragments)); `CHANGELOG.md` is frozen at 4.0.0 and isn't stamped | no notes |
| every reusable workflow (`workflow_call`) | `env: FLEET_REF: vX.Y.Z` | `main` |
| `plugins/fleet/.claude-plugin/plugin.json` | `"version": "X.Y.Z"` | `VERSION` |
| `pyproject.toml` | `[project] version = "X.Y.Z"` | `VERSION` |
| the snippets in `docs/` (fenced blocks: a `uses: AnthonyPluth/fleet/…@<commit>   # vX.Y.Z` pin comment, `"ref": "vX.Y.Z"`, `fleet sync --to vX.Y.Z`, a `fleet:begin <block> vX.Y.Z` marker) | `vX.Y.Z` | `vX.Y.Z` (the placeholder) or `v` + `VERSION` |
| fleet's own `AGENTS.md` | the `fleet:begin` markers at `vX.Y.Z` | `v` + `VERSION` |
| `tests/fixtures/` | the consumers' content (`fleet sync`) and pin comments, the drifted fixture's markers and `.fleet/version` | `v` + `VERSION` |

`fleet self-check` (in `make check`, and so in "fleet CI") fails when any of these disagree with `VERSION`: a reusable workflow's `FLEET_REF`, `plugin.json`'s or `pyproject.toml`'s version, the newest release's notes (`changelog.d/released/X.Y.Z.md`, or `CHANGELOG.md`'s newest section up to 4.0.0; from 1.0.0 on, their absence, or a date that doesn't exist), a snippet's version, or a `fleet:begin` marker in fleet's own `AGENTS.md`, which must carry every block fleet ships and no other (a missing `AGENTS.md` fails too). A snippet's value is read whole after its prefix, so a malformed one (`fleet sync --to v1.0`, `# v1.0`) fails rather than going unseen. A version is ASCII `MAJOR.MINOR.PATCH` with no leading zeros (`01.0.0` is refused), everywhere: `VERSION`, `prepare`'s argument and `release.yml`'s own check. `VERSION` holds exactly that and at most one newline after it: a space, a second newline, `\r\n` or a BOM fails `fleet self-check`, as it would fail `release.yml` after the merge. `prepare` changes all of its files or none: if anything fails part way (a fixture consumer whose markers don't parse, say), it puts every file it touched back as it was. The fixtures under `tests/fixtures/` aren't checked by `fleet self-check`; their own tests hold every fixture to one version, `v` + `VERSION`, and to the made-up commit `FIXTURE_COMMIT` (`fleet_tools/release.py`), and `prepare` moves the version. Their content is this tree's under that made-up commit, so the tests and the self-test tell `fleet drift` to take it for the running fleet's (`--own-commit`, and `checks.yml`'s self-test-only input `fixture-commit`); `fleet sync --self` re-syncs them from the tree at the version they carry. Prose outside the fenced blocks is not stamped: it may name older versions on purpose.

## Changelog fragments

Each pull request adds its changelog entry as a file of its own, a fragment in `changelog.d/`, rather than a line in a shared Unreleased section, so pull requests open at the same time never conflict over it. `CHANGELOG.md` is frozen at 4.0.0: its sections up to there stay as they are, and nothing is added to it.

```md
---
kind: added
---
`fleet x` (#123): what changed, for whom, and what an app has to do about it, on one line.
```

- **The name**, `changelog.d/<name>.md`, is lowercase letters, digits and hyphens: name it after the branch or the change (`merge-gate-auto-ready.md`), so two open pull requests don't add the same file. One pull request adds one fragment.
- **The kind** is `breaking`, `added`, `changed` or `fixed`. `breaking` is for a change a consumer has to act on ([below](#what-a-version-number-promises)); it is listed under Changed, starting with **Breaking:**. The other three are Keep a Changelog's Added, Changed and Fixed.
- **The entry** is one line (don't wrap it), written as `CHANGELOG.md`'s are, without the `- ` or the **Breaking:** that folding adds.

`fleet self-check` (so `make check`) fails on a fragment that doesn't parse, on anything else in `changelog.d/` but `released/`, and on a `## [Unreleased]` section, or one newer than 4.0.0, in `CHANGELOG.md`. On every pull request but Dependabot's, fleet CI's `make check` job then runs `fleet release check --base HEAD^1` (HEAD is the pull request merged into its base, HEAD^1 that base). It fails a pull request that adds no fragment, adds an entry to `CHANGELOG.md` (correcting one is fine) or changes a released version's notes, and any fragment that doesn't parse; a release pull request, which changes `VERSION`, adds no fragment, writes only its own notes and leaves no fragment pending in the tree it merges into: one that lands on `main` after `prepare` ran fails it, and the release is cut again from `main`. Run it on your committed branch before pushing: `poetry run fleet release check --base origin/main`.

`fleet release prepare X.Y.Z` folds the fragments into the release's notes, `changelog.d/released/X.Y.Z.md`, and deletes them. The notes are one `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`'s format: the entries under Added, Changed (the `breaking` ones first) and Fixed, each group in the order of the fragments' names. `fleet release notes X.Y.Z` prints a release's notes from there, or from `CHANGELOG.md` up to 4.0.0, and `fleet sync-plan` reads both: an app's sync pull request and migration issue list every release between the app's content and its pins, newest first, wherever its notes are, so a sync from 4.0.0 or earlier to a later release lists the old sections and the new notes together.

The fold happens in the release pull request so that the tree at each tag carries every release's notes as plain files. `release.yml`'s checkout of the release commit and `sync.yml`'s clone of fleet at the pins' tag (one commit deep) read them without git history, a second tag or any change to either workflow, and the apps' sync pull requests and migration issues get the same `## [X.Y.Z] - YYYY-MM-DD` sections as before.

## Cutting a release

1. Wait until what the release should hold has merged, and read the fragments in `changelog.d/`: the version follows from them ([below](#what-a-version-number-promises)), and a `breaking` one makes it a major.
2. On a branch, run `poetry run fleet release prepare X.Y.Z` (`--date YYYY-MM-DD` for another day than today, UTC). It refuses a version below 1.0.0, not newer than `VERSION` or not canonical, a date that doesn't exist, a release with no fragment, a minor or patch release when a fragment is `breaking`, and a tree whose stamps already disagree (a fragment that doesn't parse among them); otherwise it rewrites every stamp above, folds the fragments into the release's notes, checks them with `fleet self-check` and lists the files it changed and the fragments it folded.
3. `prepare` also re-syncs the fixture consumers (`fleet sync` into each `tests/fixtures/consumer-*` from this tree, and their fleet pin comments) and moves the drifted fixture's markers and `.fleet/version`. Run `make check`: it is green on the stamped tree, which `tests/test_release.py` rehearses on every run (a copy of the repository, `prepare` to the next version, then `fleet self-check`, `fleet drift --own-commit <the fixtures' made-up commit>` on each fixture consumer and the whole suite).
4. Open the release pull request, `chore: release vX.Y.Z`, with its notes (`poetry run fleet release notes X.Y.Z`) as its description. It goes through the merge gate and both reviews like any other (it touches every reusable workflow).
5. When it merges, `.github/workflows/release.yml` sees `VERSION` change on `main`, checks the stamps once more, tags that commit `vX.Y.Z` and publishes the GitHub release with those notes. It fails if the tag exists on another commit: a tag is never moved or reused, so a release that went wrong is followed by a new patch release, not a retag. It can be re-run: a tag already on this commit (a run whose publish failed) passes without tagging again, and a release that already exists isn't published again.
6. Dependabot then opens the apps' pin bumps (the `fleet` group, on the apps' first daily run once the release is past its 3-day cooldown, a few days after it). The owner merges each bump, or the app's merge gate does once the checks pass where the app sets `automerge-fleet-bumps: true`, and the app's `sync.yml` follows the merged bump with the content's pull request. The release commit can't name its own SHA, so nothing in fleet stamps it: the apps' `fleet sync` takes the commit from their pins (Dependabot resolved the tag to it) and writes it into `.fleet/version`, which `tools/fleet` runs fleet at.

`release.yml` runs only on a push to `main` that changes `VERSION`, from `main`'s own files, with `contents: write` and nothing else: no pull request content reaches it, its actions are pinned by SHA, the checkout keeps no credentials, and every value reaches the shell through `env`. It is in the second review's always-risky paths (`agent-review-caller.yml`). Between releases, every merge to `main` adds a fragment to `changelog.d/` and changes no version.

## What a version number promises

fleet follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html), judged from the consumer's side: what an app has to change when its pins move.

- **Major** (`2.0.0`): a consumer must change its caller or its `AGENTS.md`. An input removed or renamed, an input whose default changes what an existing setup does, a secret or permission a caller must now pass, a status or check renamed (a ruleset or merge gate names it), an `AGENTS.md` block removed or renamed, a `[tool.fleet]` key removed or its meaning changed. `sync.yml` doesn't sync a newer major by itself: it opens "fleet vN needs a migration: see CHANGELOG", so the CHANGELOG entry says what to change.
- **Minor** (`1.1.0`): new things that change nothing until a consumer asks for them, or that a consumer takes without editing anything: a new input with a default that keeps the old behaviour, a new reusable workflow, a new check (opt-in by its key, or on by default when it can't fail an app that followed the docs), a new skill, a new `AGENTS.md` block, and any change to a review's prompt or model. A prompt or model change is always called out in its changelog entry, because it changes what the reviews say without a line of the app changing.
- **Patch** (`1.0.1`): fixes that keep every documented behaviour: a bug, a message, a doc, a dependency bump.

When in doubt between two, take the larger one. Each changelog entry that needs a consumer to act starts with **Breaking** (a `breaking` fragment, which folding writes so; before 1.0.0 these read **Breaking (pre-1.0)**), and from 1.0.0 one of them makes the release a major, which `prepare` holds to.

A pull request's title still says what kind of change it is (`feat:`, `fix:`, `feat!:`), but merging it releases nothing: fleet releases only when a release pull request bumps `VERSION`.
