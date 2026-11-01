# Changelog

Every change to fleet is a change to three repositories' CI, so each one is listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and fleet uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html): a removed or renamed input, a renamed status or a removed `AGENTS.md` block is a major change unless the old behaviour stays the default.

## [Unreleased]

### Added

- The scaffold: `VERSION`, the `fleet` command (`fleet --version`, a subcommand registry, `fleet self-check`), `[tool.fleet]` config reading, `make check`, the "fleet CI" and "Self-test" workflows, and the repository's templates and policies.
- The FLEET_REF convention for reusable workflows, held by `fleet self-check`.
- The reusable merge gate (`merge-gate.yml`, `workflow_call`) and its one script (`.github/scripts/merge-gate.sh`), replacing Waypoint's and Runway's copies: inputs `sha`, `pr`, `required-workflows`, `ignored-workflows`, `required-statuses`, `ignored-apps`, `poll-seconds`, `poll-attempts`, `automerge-dependabot`, `after-merge-workflows`, `dry-run` and `fleet-ref`. A Dependabot pull request that changes an `AnthonyPluth/fleet/` reference passes the gate but is left for the owner to merge. The self-test runs it in dry run on fleet's own pull request; `tests/test_merge_gate.py` drives the script against the fake `gh` in `tests/fakes/gh`.
