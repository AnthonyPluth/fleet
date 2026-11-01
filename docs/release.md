# Releasing fleet

How a release is cut: the version bump, the `CHANGELOG.md` section, stamping `FLEET_REF` in every reusable workflow with the new tag, and the immutable tag itself. Until v1.0.0, `VERSION` is 0.x and every reusable workflow's `FLEET_REF` is `main`. Written in full by `FLEET-RELEASE`. The plugin's version (`plugins/fleet/.claude-plugin/plugin.json`) is `VERSION` too, and bumps with it (`tests/test_sync_vendor.py` checks it).
