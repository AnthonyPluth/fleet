# Changelog

Every change to fleet is a change to three repositories' CI, so each one is listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and fleet uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html): a removed or renamed input, a renamed status or a removed `AGENTS.md` block is a major change unless the old behaviour stays the default.

## [Unreleased]

### Added

- The scaffold: `VERSION`, the `fleet` command (`fleet --version`, a subcommand registry, `fleet self-check`), `[tool.fleet]` config reading, `make check`, the "fleet CI" and "Self-test" workflows, and the repository's templates and policies.
- The FLEET_REF convention for reusable workflows, held by `fleet self-check`.
- `fleet checks`: the union of Waypoint's and Runway's `tools/fleet_checks.py` (migrations, commit trailers, removed or skipped tests, workflow conventions, images, skills and generated files), configured by `[tool.fleet]`, plus the app's own `extra-checks`; `fleet no-comments` and `fleet coverage-floor` (opt-in), `fleet next-version` (replaces `next-version.sh`) and `fleet test-timings`. Keys in `docs/adopting.md`, decisions in `docs/drift.md`.
