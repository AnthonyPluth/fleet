# Sourced last by plugins/fleet/hooks/session-start.sh: fleet itself as a package (for `poetry run fleet`), the pinned
# actionlint and the locked Claude Code `make check` uses. Quick when they're already there.
if ! make --no-print-directory setup >/dev/null; then
  echo "session-start: make setup failed; run it by hand before make check" >&2
fi
