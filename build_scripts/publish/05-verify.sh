#!/bin/bash
#
# 05-verify.sh — verify the exported / notarized Blink.app before it goes
# into the DMG.
#
# Four gates:
#   [1/4] code signature     codesign --verify --deep --strict
#   [2/4] Gatekeeper policy  spctl -a -t exec
#   [3/4] share extension    BlinkShare.appex embedded, signed and sandboxed
#   [4/4] architectures      check-universal.sh on the finished .app —
#                            every Mach-O universal (x86_64 + arm64) and both
#                            slices loading the same libraries
#
# Gate 3 exists because a missing share extension is invisible: the app runs
# perfectly, Blink is simply absent from Finder's Share menu and nothing says
# why. Cheaper to fail here than to hear about it from a user.
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
echo "==> [1/4] Code signature"
echo "    codesign -vv --deep --strict $APP"
if codesign -vv --deep --strict "$APP"; then
    echo "    OK"
else
    echo "    FAILED — signature is not valid." >&2
    overall=1
fi
echo

# -------------------------------------------------------------- Gatekeeper
echo "==> [2/4] Gatekeeper assessment"
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

# --------------------------------------------------------- share extension
echo "==> [3/4] Share extension"
APPEX="$APP/Contents/PlugIns/BlinkShare.appex"
if [ ! -d "$APPEX" ]; then
    echo "    FAILED — $APPEX is missing." >&2
    echo "             The Blink target's 'Embed Share Extension' copy phase" >&2
    echo "             did not run, or the BlinkShare target did not build." >&2
    overall=1
else
    echo "    codesign -vv --strict $APPEX"
    if codesign -vv --strict "$APPEX"; then
        echo "    OK — embedded and signed"
    else
        echo "    FAILED — the extension's signature is not valid." >&2
        overall=1
    fi
    if codesign -d --entitlements - --xml "$APPEX" 2>/dev/null | grep -q "app-sandbox"; then
        echo "    OK — sandboxed"
    else
        echo "    FAILED — the extension is not sandboxed; macOS will refuse" >&2
        echo "             to load it (ShareExtension/BlinkShare.entitlements)." >&2
        overall=1
    fi
fi
echo

# ------------------------------------------------------------ architectures
echo "==> [4/4] Architectures"
echo
CHECK_INDENT="    " "$AUDIT" "$APP" || overall=1

echo
if [ "$overall" -eq 0 ]; then
    echo "==> OK — signature, Gatekeeper, share extension and architectures all pass."
    echo "    Proceed with ./06-dmg.sh"
else
    echo "==> FAILED — do not ship this build." >&2
fi
exit "$overall"
