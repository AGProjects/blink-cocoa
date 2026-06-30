#!/bin/bash
#
# 01-check.sh — pre-flight gate for the release build (run FIRST, before
# 02-archive.sh).
#
# Verify that everything is a universal binary (x86_64 + arm64) BEFORE we spend
# time archiving / notarizing / making the DMG. This catches the single-arch
# drift that has bitten us repeatedly. It runs TWO complementary checks:
#
#   1. build_scripts/11-check-macports-deps.sh — the LINK-TIME /opt/local
#      (MacPorts) libraries the sipsimple _core links. These periodically get
#      rebuilt arm64-only; the x86_64 cross-build then silently drops them and
#      the Intel app crashes at startup ("symbol not found in flat namespace").
#
#   2. build_scripts/10-check-universal-libs.sh — the BUNDLED output under
#      Distribution/Frameworks and Distribution/Resources (bundled dylibs, the
#      embedded Python framework, and the compiled .so extensions including
#      sipsimple's _core/_sha1).
#
# Cheaper to fail here than after a full notarized build that crashes on Intel.
#
# Exit status: 0 only if BOTH checks pass; non-zero (halting the publish flow)
# if either the MacPorts deps or the bundled binaries are single-arch.
#
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MACPORTS_CHECK="$SCRIPT_DIR/../11-check-macports-deps.sh"
BUNDLE_CHECK="$SCRIPT_DIR/../10-check-universal-libs.sh"

for c in "$MACPORTS_CHECK" "$BUNDLE_CHECK"; do
    if [ ! -x "$c" ]; then
        echo "error: required checker not found or not executable:" >&2
        echo "       $c" >&2
        exit 1
    fi
done

overall=0

echo "==> [1/2] Link-time check: MacPorts (/opt/local) deps of sipsimple _core ..."
echo
"$MACPORTS_CHECK"
rc1=$?
[ "$rc1" -ne 0 ] && overall=1

echo
echo "==> [2/2] Bundle check: all binaries under Distribution/ are universal ..."
echo
"$BUNDLE_CHECK"
rc2=$?
[ "$rc2" -ne 0 ] && overall=1

echo
if [ "$overall" -eq 0 ]; then
    echo "==> OK — MacPorts deps and bundle are fully universal. Proceed with ./02-archive.sh"
else
    echo "==> FAILED — fix the single-arch items above before archiving." >&2
    echo "    MacPorts deps: reinstall the port +universal (see 02-install-c-deps.sh)," >&2
    echo "    then re-run 04-install_sipsimple.sh and 05-copy-libraries.sh." >&2
fi
exit "$overall"
