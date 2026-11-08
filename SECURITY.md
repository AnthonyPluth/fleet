# Security

## Reporting a vulnerability

Please report it privately through GitHub: the repository's **Security** tab → **Report a vulnerability** ([open a private report](https://github.com/AnthonyPluth/fleet/security/advisories/new)). Don't open a public issue, pull request or discussion for a security problem. Include what you found, how to reproduce it, and the fleet version or commit. You'll get an answer within a week, and a fix is released as soon as it's ready.

## Supported versions

Only the latest release gets security fixes. The apps pick it up through Dependabot's update of their fleet pins.

## The trust model

fleet's reusable workflows run inside the consumers, with the consumers' secrets and token permissions: a workflow an app calls from here is, for every purpose that matters, that app's own workflow. The same goes for the scripts, prompts and tools those workflows fetch from fleet at their own commit (the one the app pins), and for what `fleet sync` vendors into each app. So a change here is reviewed as a change to every consumer's own workflows:

- Everything is pinned. The apps call fleet by commit SHA (with a `# vX.Y.Z` comment), never by branch; fleet pins every action it uses by SHA too, and its release tags don't move.
- Workflows ask for the least they need: `permissions: {}` at the top, then per job; `persist-credentials: false` on every checkout; nothing from a pull request (title, body, branch name, file contents) is expanded into a shell command.
- Secrets are passed by name by each caller, never inherited wholesale, and nothing here prints, stores or forwards them.
- A pull request from outside the repository's owner is untrusted content: workflows that read it never run it, and a review of it never gets write access or secrets beyond what it needs.

zizmor and actionlint run on every pull request (`make check`), and the review of each pull request checks that nothing personal (a household member, a host, an account, a token) reaches the repository.
