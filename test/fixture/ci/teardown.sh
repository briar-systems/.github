#!/usr/bin/env bash
# proves teardown follows setup
set -euo pipefail
[ -f "$RUNNER_TEMP/fixture-setup" ] || { echo "::error::teardown ran without setup"; exit 1; }
rm "$RUNNER_TEMP/fixture-setup"
