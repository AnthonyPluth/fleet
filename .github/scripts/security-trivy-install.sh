#!/usr/bin/env bash
# Installs Trivy for the `trivy` and `image` jobs of security.yml, in place of aquasecurity/setup-trivy (whose install
# script downloads from get.trivy.dev, which stopped resolving): the release tarball straight from Trivy's GitHub
# release, checked against the SHA-256 security.yml pins for the runner's platform, then put on the PATH. A tarball
# already in TRIVY_CACHE (restored by actions/cache) is used only when its checksum still matches; any other is
# downloaded again and checked the same way. It writes `installed=true` to GITHUB_OUTPUT once Trivy is on the PATH.
# On a platform with no pinned checksum (anything but Linux on X64 or ARM64) it installs nothing and writes
# `installed=false`, and the job falls back to trivy-action's own install as before. On a pinned platform a failed
# download or a checksum that doesn't match fails the step: it never falls back.
#
# Environment: TRIVY_VERSION (v0.70.0), TRIVY_SHA256_X64 and TRIVY_SHA256_ARM64 (the tarballs' pinned checksums),
# TRIVY_CACHE (where the tarball is kept), TRIVY_BIN (where the binary goes), RUNNER_OS, RUNNER_ARCH, GITHUB_PATH and
# GITHUB_OUTPUT (set by the runner), TRIVY_DOWNLOAD (the release download URL; default GitHub's, for the tests).
set -euo pipefail

TRIVY_DOWNLOAD=${TRIVY_DOWNLOAD:-https://github.com/aquasecurity/trivy/releases/download}

case "${RUNNER_OS:-}-${RUNNER_ARCH:-}" in
  Linux-X64) platform=Linux-64bit; sum=${TRIVY_SHA256_X64:-} ;;
  Linux-ARM64) platform=Linux-ARM64; sum=${TRIVY_SHA256_ARM64:-} ;;
  *)
    echo "::warning::No pinned Trivy for ${RUNNER_OS:-?} on ${RUNNER_ARCH:-?} (only Linux on X64 or ARM64): trivy-action installs it instead"
    echo "installed=false" >> "$GITHUB_OUTPUT"
    exit 0 ;;
esac

if ! [[ "${TRIVY_VERSION:-}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "::error::TRIVY_VERSION should look like v0.70.0, not '${TRIVY_VERSION:-}'" >&2
  exit 1
fi
if ! [[ "$sum" =~ ^[0-9a-f]{64}$ ]]; then
  echo "::error::No pinned SHA-256 for Trivy's $platform tarball" >&2
  exit 1
fi

version=${TRIVY_VERSION#v}
tarball="trivy_${version}_${platform}.tar.gz"
mkdir -p "$TRIVY_CACHE" "$TRIVY_BIN"
cached="$TRIVY_CACHE/$tarball"

matches() {
  echo "$sum  $1" | sha256sum --check --status -
}

if [ -f "$cached" ] && matches "$cached"; then
  echo "Using the cached $tarball (SHA-256 checked)."
else
  if [ -f "$cached" ]; then
    echo "::warning::The cached $tarball doesn't match its pinned SHA-256; downloading it again"
  fi
  rm -f "$cached" "$cached.part"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --retry 3 \
    --output "$cached.part" "$TRIVY_DOWNLOAD/$TRIVY_VERSION/$tarball"
  if ! matches "$cached.part"; then
    rm -f "$cached.part"
    echo "::error::$tarball doesn't match its pinned SHA-256 ($sum); not installing it" >&2
    exit 1
  fi
  mv "$cached.part" "$cached"
fi

tar --extract --gzip --file "$cached" --directory "$TRIVY_BIN" trivy
chmod 0755 "$TRIVY_BIN/trivy"
echo "$TRIVY_BIN" >> "$GITHUB_PATH"
"$TRIVY_BIN/trivy" --version
echo "installed=true" >> "$GITHUB_OUTPUT"
