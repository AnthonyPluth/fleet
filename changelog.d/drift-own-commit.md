---
kind: added
---
`fleet drift --own-commit <sha>` and a self-test-only input to `.github/workflows/checks.yml`, `fixture-commit` (string, default empty, which keeps the drift step as it was): the commit to take for the running fleet's own, for fleet's fixture consumers, whose content is fleet's tree under a made-up commit. The self-test's three checks calls and the release rehearsal pass it.
