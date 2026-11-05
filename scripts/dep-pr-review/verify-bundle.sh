#!/usr/bin/env bash
# Checks a downloaded bundle against the manifest the collector wrote:
# every listed file present with its sha256, and no file that is not listed.
# Hidden files count too (upload-artifact drops them unless told otherwise).
set -euo pipefail
bundle=${1:?usage: verify-bundle.sh BUNDLE_DIR}
cd "$bundle"
if [ ! -s MANIFEST.sha256 ]; then
  echo "::error::bundle has no MANIFEST.sha256"
  exit 1
fi
sha256sum --check --strict --quiet MANIFEST.sha256
listed=$(wc -l < MANIFEST.sha256)
present=$(find . -type f ! -path ./MANIFEST.sha256 | wc -l)
if [ "$listed" -ne "$present" ]; then
  echo "::error::bundle holds $present files but its manifest lists $listed"
  exit 1
fi
echo "bundle complete: $listed files match MANIFEST.sha256"
