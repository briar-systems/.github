#!/usr/bin/env bash
# proves the hook contract: every variable is set before the first mach command
set -euo pipefail
for name in MACH_COMPILER MACH_CI_LEG MACH_CI_TIER MACH_CI_PRIMARY MACH_CI_PROJECT MACH_CI_PROFILES; do
  [ -n "${!name:-}" ] || { echo "::error::setup hook saw no $name"; exit 1; }
done
"$MACH_COMPILER" info --version
touch "$RUNNER_TEMP/fixture-setup"
