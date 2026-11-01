# Drift between the apps

Where Waypoint and Runway did the same thing differently, and which way fleet took. Each extraction issue adds its rows. Where the two differ, fleet's own code follows the stricter convention (Waypoint's): no comments or docstrings in Python, actions pinned by SHA with a version comment, `bash -eo pipefail` as each workflow's default shell, and `persist-credentials: false` on every checkout.
