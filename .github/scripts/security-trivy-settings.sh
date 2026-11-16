#!/usr/bin/env bash
# Keeps the checkout's own Trivy settings out of the `trivy` and `image` jobs of security.yml. Trivy (v0.70.0) reads
# three files from its working directory when they exist, and in both jobs that directory is the caller's checkout,
# which on a pull request is the pull request's own merge commit:
#   ./trivy.yaml         (--config, TRIVY_CONFIG): any option, among them skip-files, skip-dirs, ignorefile, severity
#   ./.trivyignore       (--ignorefile, TRIVY_IGNOREFILE): ids of findings to drop, of every scanner
#   ./trivy-secret.yaml  (--secret-config, TRIVY_SECRET_CONFIG): secret rules to disable, paths and patterns to allow
# So a pull request that added one could drop its own findings. This script writes an empty one of each into
# TRIVY_SETTINGS, outside the workspace, for the scan step to name in TRIVY_CONFIG, TRIVY_IGNOREFILE and
# TRIVY_SECRET_CONFIG: Trivy then reads those and never looks for the three names in its working directory, whatever
# is there by the time it runs. That alone is enough. The empty files exist and parse because Trivy fails on a named
# ignore file that isn't there and on a secret config with no YAML document in it. The empty secret config is a file
# named .git: Trivy's secret scan skips the file at the scan's root whose name is the secret config's own, so under
# the name trivy-secret.yaml the checkout's trivy-secret.yaml would never be scanned for secrets, and under any other
# ordinary name a file of that name would not. No checkout holds a file of its own named .git (git refuses the path,
# and the root's .git is the repository, which Trivy doesn't walk), so the skip falls on nothing.
#
# What happens to the checkout's own three is CHECKOUT_SETTINGS, and each one found gets a warning naming it:
#   keep    (the file scan) they stay where they are, so the scan reads them as it reads any other file: a secret
#           committed in one (a registry password in trivy.yaml, say) is reported. Removing them would hide it.
#   remove  (the image scan, which scans the built image and not the checkout) they go, a link or a directory of that
#           name too, as a second barrier: nothing is lost, since that scan never read the checkout's files.
#
# It runs on every event, not only on a pull request: a scan of main reads what a pull request's scan reads, so
# nothing a pull request can't use appears to work once merged.
#
# Environment: TRIVY_SETTINGS (an absolute directory outside the working directory; emptied and written),
# CHECKOUT_SETTINGS (keep or remove).
set -euo pipefail

settings=${TRIVY_SETTINGS:-}
case "$settings" in
  /*) ;;
  *) echo "::error::TRIVY_SETTINGS should be an absolute directory, not '$settings'" >&2; exit 1 ;;
esac
checkout=${CHECKOUT_SETTINGS:-}
case "$checkout" in
  keep|remove) ;;
  *) echo "::error::CHECKOUT_SETTINGS should be keep or remove, not '$checkout'" >&2; exit 1 ;;
esac
here=$(pwd -P)
resolved=$(realpath --canonicalize-missing -- "$settings")
case "$resolved/" in
  "$here"/*) echo "::error::TRIVY_SETTINGS is inside the working directory, where the checkout could reach it" >&2; exit 1 ;;
esac

for name in trivy.yaml .trivyignore trivy-secret.yaml; do
  if [ -e "./$name" ] || [ -L "./$name" ]; then
    if [ "$checkout" = remove ]; then
      rm -rf -- "./$name"
    fi
    echo "::warning title=Trivy settings not read::This checkout has $name. The security scans don't take Trivy's settings from it, since a pull request could add one to drop its own findings; the file scan reads it like any other file, for secrets too: delete it. A finding the owner accepts for the image goes in the list that trivy-image-ignore-file names (docs/adopting.md, Accepted image findings)."
  fi
done

rm -rf -- "$resolved"
mkdir -p -- "$resolved"
printf '{}\n' > "$resolved/trivy.yaml"
: > "$resolved/trivyignore"
printf '{}\n' > "$resolved/.git"
echo "Trivy reads its settings from $resolved (empty), not from the checkout."
