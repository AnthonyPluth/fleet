#!/usr/bin/env bash
# Keeps the checkout's own Trivy settings out of the `trivy` and `image` jobs of security.yml. Trivy (v0.70.0) reads
# three files from its working directory when they exist, and in both jobs that directory is the caller's checkout,
# which on a pull request is the pull request's own merge commit:
#   ./trivy.yaml         (--config, TRIVY_CONFIG): any option, among them skip-files, skip-dirs, ignorefile, severity
#   ./.trivyignore       (--ignorefile, TRIVY_IGNOREFILE): ids of findings to drop, of every scanner
#   ./trivy-secret.yaml  (--secret-config, TRIVY_SECRET_CONFIG): secret rules to disable, paths and patterns to allow
# So a pull request that added one could drop its own findings. This script does two things, each enough by itself:
#   1. it removes the three from the working directory, with a warning naming each one it found (a link or a
#      directory of that name goes too);
#   2. it writes an empty one of each into TRIVY_SETTINGS, outside the workspace, for the scan step to name in
#      TRIVY_CONFIG, TRIVY_IGNOREFILE and TRIVY_SECRET_CONFIG: Trivy then reads those and never looks for the three
#      names in its working directory, whatever is there by the time it runs.
# The empty files exist and parse because Trivy fails on a named ignore file that isn't there and on a secret config
# with no YAML document in it.
#
# It runs on every event, not only on a pull request: a scan of main reads what a pull request's scan reads, so
# nothing a pull request can't use appears to work once merged.
#
# Environment: TRIVY_SETTINGS (an absolute directory outside the working directory; emptied and written).
set -euo pipefail

settings=${TRIVY_SETTINGS:-}
case "$settings" in
  /*) ;;
  *) echo "::error::TRIVY_SETTINGS should be an absolute directory, not '$settings'" >&2; exit 1 ;;
esac
here=$(pwd -P)
resolved=$(realpath --canonicalize-missing -- "$settings")
case "$resolved/" in
  "$here"/*) echo "::error::TRIVY_SETTINGS is inside the working directory, where the checkout could reach it" >&2; exit 1 ;;
esac

for name in trivy.yaml .trivyignore trivy-secret.yaml; do
  if [ -e "./$name" ] || [ -L "./$name" ]; then
    rm -rf -- "./$name"
    echo "::warning title=Trivy settings not read::This checkout has $name. The security scans don't read it, since a pull request could add one to drop its own findings, and scan as if it weren't there: delete it. A finding the owner accepts for the image goes in the list that trivy-image-ignore-file names (docs/adopting.md, Accepted image findings)."
  fi
done

rm -rf -- "$resolved"
mkdir -p -- "$resolved"
printf '{}\n' > "$resolved/trivy.yaml"
: > "$resolved/trivyignore"
printf '{}\n' > "$resolved/trivy-secret.yaml"
echo "Trivy reads its settings from $resolved (empty), not from the checkout."
