#!/usr/bin/env bash
# proves the tier reached the leg and the standard phases produced output
set -euo pipefail
case "${GITHUB_BASE_REF:-}" in
  main) want=heavy ;;
  dev) want=light ;;
  *) want=$MACH_CI_TIER ;;
esac
[ "$MACH_CI_TIER" = "$want" ] || { echo "::error::a pull request into ${GITHUB_BASE_REF} ran leg $MACH_CI_LEG as $MACH_CI_TIER"; exit 1; }
[ -f "$RUNNER_TEMP/fixture-setup" ] || { echo "::error::verify ran without setup"; exit 1; }
for profile in $MACH_CI_PROFILES; do
  found=$(find "$MACH_CI_PROJECT/out" -path "*/$profile/lib/*" -type f | head -n 1)
  [ -n "$found" ] || { echo "::error::no $profile library under $MACH_CI_PROJECT/out"; exit 1; }
done
if [ "$MACH_CI_PRIMARY" = true ]; then
  [ -d "$MACH_CI_PROJECT/out/windows-x86_64/release" ] || { echo "::error::the primary leg did not build every target"; exit 1; }
fi
echo "leg $MACH_CI_LEG ran $MACH_CI_TIER"
