---
kind: added
---
`fleet sync --self` (#112): fleet's own `AGENTS.md` blocks and its fixture consumers (`tests/fixtures/consumer-*`) re-synced from its tree, each at the version its markers and `.fleet/version` already carry (`fleet release prepare` still moves those), all or nothing. It replaces `fleet sync --to v$(cat VERSION)` in fleet's own checkout, which `sync --to` now refuses on any commit but the release's.
