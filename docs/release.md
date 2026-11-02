# Releasing fleet

The apps call fleet by pinned reference: each caller pins a reusable workflow by commit SHA with the tag in a comment (`@<sha>   # vX.Y.Z`), Dependabot bumps those pins, and the reusable workflows fetch fleet's own scripts, prompts and tools at the tag stamped into them (`FLEET_REF`). So a release is one commit where every version agrees, tagged once and never moved.

## What a release stamps

| Where | What | Before v1.0.0 |
| --- | --- | --- |
| `VERSION` | `X.Y.Z` | `0.x` |
| `CHANGELOG.md` | the Unreleased entries become `## [X.Y.Z] - YYYY-MM-DD`, under a fresh empty `## [Unreleased]` | no released section |
| every reusable workflow (`workflow_call`) | `env: FLEET_REF: vX.Y.Z` | `main` |
| `plugins/fleet/.claude-plugin/plugin.json` | `"version": "X.Y.Z"` | `VERSION` |
| `pyproject.toml` | `[project] version = "X.Y.Z"` | `VERSION` |
| the snippets in `docs/` (fenced blocks: a `uses: AnthonyPluth/fleet/…@<commit>   # vX.Y.Z` pin comment, `"ref": "vX.Y.Z"`, `fleet sync --to vX.Y.Z`, a `fleet:begin <block> vX.Y.Z` marker, a fleet URL or `echo vX.Y.Z > .fleet/version`) | `vX.Y.Z` | `vX.Y.Z` (the placeholder) or `v` + `VERSION` |
| fleet's own `AGENTS.md` | the `fleet:begin` markers at `vX.Y.Z` | `v` + `VERSION` |
| `tests/fixtures/` | the consumers' content (`fleet sync`) and pin comments, the drifted fixture's markers and `.fleet/version` | `v` + `VERSION` |

`fleet self-check` (in `make check`, and so in "fleet CI") fails when any of these disagree with `VERSION`: a reusable workflow's `FLEET_REF`, `plugin.json`'s or `pyproject.toml`'s version, the newest released `CHANGELOG.md` section (and from 1.0.0 on, its absence, or a date that doesn't exist), a snippet's version, or a `fleet:begin` marker in fleet's own `AGENTS.md` (a missing `AGENTS.md` fails too). A snippet's value is read whole after its prefix, so a malformed one (`fleet sync --to v1.0`, `# v1.0`) fails rather than going unseen. A version is ASCII `MAJOR.MINOR.PATCH` with no leading zeros (`01.0.0` is refused), everywhere: `VERSION`, `prepare`'s argument and `release.yml`'s own check. The fixtures under `tests/fixtures/` aren't checked by `fleet self-check`; their own tests hold them to `VERSION`, and `prepare` moves them. Prose outside the fenced blocks is not stamped: it may name older versions on purpose.

## Cutting a release

1. Wait until what the release should hold has merged, and read `CHANGELOG.md`'s Unreleased section: the version follows from it ([below](#what-a-version-number-promises)).
2. On a branch, run `poetry run fleet release prepare X.Y.Z` (`--date YYYY-MM-DD` for another day than today, UTC). It refuses a version below 1.0.0, not newer than `VERSION` or not canonical, a date that doesn't exist, an empty Unreleased section, a version that already has a section, and a tree whose stamps already disagree; otherwise it rewrites every stamp above, checks them with `fleet self-check` and lists the files it changed.
3. `prepare` also re-syncs the fixture consumers (`fleet sync` into each `tests/fixtures/consumer-*` from this tree, and their fleet pin comments) and moves the drifted fixture's markers and `.fleet/version`, so the tests that hold them to `VERSION` pass. Run `make check`: it is green on the stamped tree, which `tests/test_release.py` rehearses on every run (a copy of the repository, `prepare` to the next version, then `fleet self-check`, `fleet drift` on each fixture consumer and the whole suite).
4. Open the release pull request, `chore: release vX.Y.Z`, with the CHANGELOG section as its description. It goes through the merge gate and both reviews like any other (it touches every reusable workflow).
5. When it merges, `.github/workflows/release.yml` sees `VERSION` change on `main`, checks the stamps once more, tags that commit `vX.Y.Z` and publishes the GitHub release with that CHANGELOG section as its notes. It fails if the tag exists on another commit: a tag is never moved or reused, so a release that went wrong is followed by a new patch release, not a retag. It can be re-run: a tag already on this commit (a run whose publish failed) passes without tagging again, and a release that already exists isn't published again.
6. Dependabot then opens the apps' pin bumps (the `fleet` group, after its cooldown), and each app's `sync.yml` follows a merged bump with the content's pull request.

`release.yml` runs only on a push to `main` that changes `VERSION`, from `main`'s own files, with `contents: write` and nothing else: no pull request content reaches it, its actions are pinned by SHA, the checkout keeps no credentials, and every value reaches the shell through `env`. It is in the second review's always-risky paths (`agent-review-caller.yml`). Between releases, every merge to `main` adds to the Unreleased section and changes no version.

## What a version number promises

fleet follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html), judged from the consumer's side: what an app has to change when its pins move.

- **Major** (`2.0.0`): a consumer must change its caller or its `AGENTS.md`. An input removed or renamed, an input whose default changes what an existing setup does, a secret or permission a caller must now pass, a status or check renamed (a ruleset or merge gate names it), an `AGENTS.md` block removed or renamed, a `[tool.fleet]` key removed or its meaning changed. `sync.yml` doesn't sync a newer major by itself: it opens "fleet vN needs a migration: see CHANGELOG", so the CHANGELOG entry says what to change.
- **Minor** (`1.1.0`): new things that change nothing until a consumer asks for them, or that a consumer takes without editing anything: a new input with a default that keeps the old behaviour, a new reusable workflow, a new check (opt-in by its key, or on by default when it can't fail an app that followed the docs), a new skill, a new `AGENTS.md` block, and any change to a review's prompt or model. A prompt or model change is always called out in its CHANGELOG entry, because it changes what the reviews say without a line of the app changing.
- **Patch** (`1.0.1`): fixes that keep every documented behaviour: a bug, a message, a doc, a dependency bump.

When in doubt between two, take the larger one. Each CHANGELOG entry that needs a consumer to act starts with **Breaking**; before 1.0.0 these read **Breaking (pre-1.0)**, and from 1.0.0 one of them makes the release a major.

A pull request's title still says what kind of change it is (`feat:`, `fix:`, `feat!:`), but merging it releases nothing: fleet releases only when a release pull request bumps `VERSION`.
