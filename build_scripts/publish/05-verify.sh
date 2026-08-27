#!/bin/bash
#
# 05-verify.sh — verify the exported / notarized Blink.app before it goes
# into the DMG.
#
# Three gates:
#   [1/3] code signature     codesign --verify --deep --strict
#   [2/3] Gatekeeper policy  spctl -a -t exec
#   [3/3] architectures      check-universal.sh on the finished .app —
#                            every Mach-O universal (x86_64 + arm64) and both
#                            slices loading the same libraries
#
# Gate 3 is the same audit 01-check.sh runs before archiving; the difference
# is what it points at. 01-check.sh audits the staged payload under
# Distribution/ so a problem is caught before archive + notarization; this
# runs on the actual shipping artifact, after Xcode has copied, re-signed and
# stapled it — the last chance to catch something the pipeline introduced
# along the way.
#
# See check-universal.sh for what the architecture audit covers and how
# slice differences are classified.
#
# Usage:  ./05-verify.sh [/path/to/Blink.app]
# Exit:   0 = all gates pass, 1 = at least one failed.
#
# https://developer.apple.com/library/archive/technotes/tn2206/_index.html

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${1:-$SCRIPT_DIR/../../Distribution/Notary/Blink.app}"
AUDIT="$SCRIPT_DIR/check-universal.sh"

if [ ! -d "$APP" ]; then
    echo "error: app bundle not found: $APP" >&2
    echo "       run ./03-export.sh (and ./04-notarize.sh) first." >&2
    exit 1
fi

if [ ! -x "$AUDIT" ]; then
    echo "error: required checker not found or not executable:" >&2
    echo "       $AUDIT" >&2
    exit 1
fi

overall=0

# --------------------------------------------------------------- signature
echo "==> [1/3] Code signature"
echo "    codesign -vv --deep --strict $APP"
if codesign -vv --deep --strict "$APP"; then
    echo "    OK"
else
    echo "    FAILED — signature is not valid." >&2
    overall=1
fi
echo

# -------------------------------------------------------------- Gatekeeper
echo "==> [2/3] Gatekeeper assessment"
echo "    spctl -a -t exec -vvv $APP"
if spctl -a -t exec -vvv "$APP"; then
    echo "    OK"
else
    echo "    FAILED — Gatekeeper rejected the bundle." >&2
    echo "             Not notarized, or the ticket was never stapled" >&2
    echo "             (see ./04-notarize.sh)." >&2
    overall=1
fi
echo

# ------------------------------------------------------------ architectures
echo "==> [3/3] Architectures"
echo
CHECK_INDENT="    " "$AUDIT" "$APP" || overall=1

echo
if [ "$overall" -eq 0 ]; then
    echo "==> OK — signature, Gatekeeper and architectures all pass."
    echo "    Proceed with ./06-dmg.sh"
else
    echo "==> FAILED — do not ship this build." >&2
fi
exit "$overall"
