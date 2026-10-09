#!/usr/bin/env bash
# Fill openswap.rb.in with a release's version and per-architecture checksums.
#
#   packaging/homebrew/render-cask.sh 0.2.0 <arm64-sha256> <x86_64-sha256> > openswap.rb
#
# The checksums are of OpenSwap-<version>-arm64.zip and
# OpenSwap-<version>-x86_64.zip. The release workflow runs this and attaches
# the result to the GitHub release; the tap workflow copies that file into
# mar3co/homebrew-openswap.
set -euo pipefail

usage="usage: render-cask.sh <version> <arm64-sha256> <x86_64-sha256>"
if [[ $# -ne 3 ]]; then
  echo "render-cask.sh: $usage" >&2
  exit 2
fi
version="$1"
sha256_arm64="$2"
sha256_x86_64="$3"
if [[ ! "$version" =~ ^[0-9A-Za-z][0-9A-Za-z.+-]*$ ]]; then
  echo "render-cask.sh: not a version: $version" >&2
  exit 1
fi
for sha256 in "$sha256_arm64" "$sha256_x86_64"; do
  if [[ ! "$sha256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "render-cask.sh: not a sha256: $sha256" >&2
    exit 1
  fi
done

HERE="$(cd "$(dirname "$0")" && pwd)"
sed -e "s/@VERSION@/$version/" \
  -e "s/@SHA256_ARM64@/$sha256_arm64/" \
  -e "s/@SHA256_X86_64@/$sha256_x86_64/" \
  "$HERE/openswap.rb.in"
