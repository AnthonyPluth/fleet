## fleet

fleet is the shared agent build safeguards of three apps (Darkroom, Waypoint and Runway): reusable GitHub Actions workflows, the `fleet` command (`fleet_tools/`, standard library only), the Claude Code plugin and the shared `AGENTS.md` blocks. The apps call it by pinned reference, so every change here is a change to three repositories' CI, and a workflow or script change is the riskiest kind: read `AGENTS.md`'s "Rules" closely, "The FLEET_REF convention" and "fleet follows the stricter convention" above all.

## Private data here

The repository is public and holds no app data. Private data would be anything from a real instance of one of the apps or about its owner: a household member's or traveller's name, an email address, a hostname or domain that belongs to someone, an account, a token, a key or a URL with one in it, a real booking, transaction or photo. The fixtures under `tests/fixtures/` (the consumer trees `consumer-waypoint/` and `consumer-runway/`, the security scan trees and the workflow fixtures) and the fakes under `tests/fakes/` use made-up values (`example/app`, `octocat`, `pat@example.com`); a real-looking name, address, host, token or email beside them is a blocking finding. Screenshots and other images aren't expected here at all: a committed raster image is a finding until explained.

## Generated files and the checks that cover them

None: fleet has no generated files, and `omitted.txt` lists only lockfiles (`poetry.lock`, covered by `poetry install` in CI) and images. `.github/agent-review/package-lock.json` is always in the diff, like everything under `.github/`.

## Where things are

`fleet_tools/` is the command, with `checks/` one module per check; `.github/workflows/` the reusable workflows beside fleet's own CI and self-test; `.github/scripts/` and `.github/agent-review/` what those workflows run; `tests/` the `unittest` suite. A change to a reusable workflow's inputs, a status name or an `AGENTS.md` block is a breaking change unless the old behaviour stays the default, and every change adds a line to `CHANGELOG.md`'s Unreleased section.
