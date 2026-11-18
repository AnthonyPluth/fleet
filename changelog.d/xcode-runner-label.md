---
kind: added
---
`fleet checks` accepts a pinned Xcode runner for a job that only macOS can build: `runs-on: xcode-<number>   # hosted: <why>` (for example `xcode-27   # hosted: the iOS 27 SDK needs Xcode 27`) passes as `macos-<number>` does, with the same `# hosted:` reason required, for an Xcode newer than the `macos-<number>` images carry. The number is ASCII digits only: `xcode-latest`, `xcode-`, a suffix (`xcode-27-beta`, `xcode-27-xlarge`), anything after the number but the comment, and an `xcode-<number>` runner without the reason still fail. Every other runner rule and check is unchanged. What apps do: pin to the release, then use `runs-on: xcode-<number>   # hosted: <why>` where a job needs that Xcode. `docs/adopting.md`; `tests/test_checks_workflows.py`.
