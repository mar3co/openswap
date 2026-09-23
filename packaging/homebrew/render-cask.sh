#!/usr/bin/env bash
# Fill openswap.rb.in with a release's version and zip checksum.
#
#   packaging/homebrew/render-cask.sh 0.2.0 <sha256> > openswap.rb
#
# The release workflow runs this and attaches the result to the GitHub
# release; the tap workflow copies that file into mar3co/homebrew-openswap.
set -euo pipefail

version="${1:?usage: render-cask.sh <version> <sha256>}"
sha256="${2:?usage: render-cask.sh <version> <sha256>}"
if [[ ! "$sha256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "render-cask.sh: not a sha256: $sha256" >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
sed -e "s/@VERSION@/$version/" -e "s/@SHA256@/$sha256/" "$HERE/openswap.rb.in"
