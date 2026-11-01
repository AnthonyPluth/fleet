#!/usr/bin/env bash
# Semgrep for the `semgrep` job of security.yml: the registry packs plus the app's own rules (every .semgrep/*.yml),
# after the app's example check. With SARIF_UPLOAD=true (the default) the findings go to semgrep.sarif for the
# Security tab, less those suppressed in source with `nosemgrep`; with SARIF_UPLOAD=false Semgrep fails this step
# on any finding instead (--error), for a repository without code scanning.
#
# Environment: ROOT (the tree to scan; default .), SEMGREP_PATHS (space-separated targets within it; default .),
# SARIF_UPLOAD, SEMGREP_EXAMPLES (a command to run first; empty runs .semgrep/check_examples.py when present),
# SEMGREP (the command; default pipx run semgrep at the pinned version), DROP_SUPPRESSED (fleet's drop_suppressed.py).
set -euo pipefail

ROOT=${ROOT:-.}
SEMGREP_PATHS=${SEMGREP_PATHS:-.}
SARIF_UPLOAD=${SARIF_UPLOAD:-true}
SEMGREP_EXAMPLES=${SEMGREP_EXAMPLES:-}
SEMGREP=${SEMGREP:-pipx run semgrep==1.146.0}
DROP_SUPPRESSED=${DROP_SUPPRESSED:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/drop_suppressed.py}

cd "$ROOT"
read -ra semgrep <<< "$SEMGREP"
read -ra targets <<< "$SEMGREP_PATHS"

if [ -n "$SEMGREP_EXAMPLES" ]; then
  bash -c "$SEMGREP_EXAMPLES"
elif [ -f .semgrep/check_examples.py ]; then
  python3 .semgrep/check_examples.py "${semgrep[@]}"
fi

configs=(--config p/python --config p/security-audit --config p/owasp-top-ten --config p/javascript)
for rules in .semgrep/*.yml .semgrep/*.yaml; do
  [ -f "$rules" ] && configs+=(--config "$rules")
done

# --project-root makes ROOT the tree whose .gitignore and .semgrepignore apply (Semgrep's default list skips `tests/`,
# which is where fleet's self-test keeps the planted tree it scans).
options=(--metrics=off --disable-version-check --project-root .)
if [ "$SARIF_UPLOAD" = true ]; then
  "${semgrep[@]}" scan "${options[@]}" --sarif --output semgrep.sarif "${configs[@]}" "${targets[@]}"
  python3 "$DROP_SUPPRESSED" semgrep.sarif
else
  "${semgrep[@]}" scan "${options[@]}" --error "${configs[@]}" "${targets[@]}"
fi
