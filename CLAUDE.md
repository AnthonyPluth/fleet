# Claude Code notes

Repo conventions, commands and layout are in [AGENTS.md](AGENTS.md); read it first.

## Model routing

When delegating to a subagent, set its `model` accordingly:

- **Opus**, at `xhigh` effort (set the Agent tool's `effort: xhigh`): the default, for everything not listed below, including anything touching the review workflows, secrets, untrusted pull request content or security scanning.
- **Sonnet**: only for mechanical edits (renames, typo fixes, moving text without changing it).
