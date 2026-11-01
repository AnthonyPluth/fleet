# Skills

A skill fixed once helps every session only if there is one copy of it. So an app's skills have two homes, and `fleet checks` (`skill-homes`, wherever `[tool.fleet]` sets `skills`) keeps them to those two.

## Where skills live

| Home | Holds | Changed in |
| --- | --- | --- |
| `.claude/skills/fleet-<name>/` | fleet's skills, vendored by `fleet sync` from `plugins/fleet/skills/<name>/` ([adopting](adopting.md#claude-code-plugin)) | fleet, never the app |
| `.claude/skills/<name>/` | the app's own skills (`verify-waypoint`, say) | the app |

Skills go nowhere else: not under another tool's directory, not nested deeper, not at the root. `skill-homes` reports:

- a `SKILL.md` that git tracks (or would: one that isn't ignored) anywhere but `<skills>/<name>/SKILL.md`, except under `node_modules/`, `.fleet/`, `tests/fixtures/` and the globs in `skill-homes-ignore` (default none; `*` matches across `/`);
- a `<skills>/fleet-*` directory whose `SKILL.md` lacks the line `fleet sync` writes after the front matter's opening `---`: `fleet-` is reserved for vendored skills, so an app's own skill takes another name;
- an app skill whose front matter's `name` is a fleet skill's, with or without the `fleet-` prefix;
- an app skill whose body (the front matter left out) is a fleet skill's after whitespace is normalized: copied rather than vendored.

fleet's skills are the vendored `fleet-*` ones in the app and those of the fleet running the check.

## When an app skill moves into fleet

When a second app would use it, and nothing app-specific is left in it that `[tool.fleet]` or `.claude/hooks/fleet.conf` can't carry. Until then it stays the app's own.

## How it moves

1. A fleet pull request adds it as `plugins/fleet/skills/<name>/SKILL.md` (with a `CHANGELOG.md` line), and re-runs `fleet sync` on the fixture consumers.
2. A fleet release ships it.
3. The app's sync pull request (`fleet sync --to vX.Y.Z`) vendors it as `.claude/skills/fleet-<name>/` and deletes the app's own copy in the same change, so there is never a second home, even for one commit.

Each move is an issue of its own.
