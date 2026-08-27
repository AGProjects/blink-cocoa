#!/bin/bash
#
# 01-check.sh — pre-flight gate for the release build (run FIRST, before
# 02-archive.sh).
#
# Scope: THE PRODUCT ONLY. This script audits exactly what Xcode copies into
# Blink.app and nothing else. Everything about the build environment
# (/opt/local MacPorts deps, the venv, the sipsimple source tree) belongs to
# the build phase and is checked there:
#
#     build_scripts/11-check-macports-deps.sh   /opt/local deps of _core
#     build_scripts/10-check-universal-libs.sh  broad sweep of Distribution/
#
# Run those while building. By the time you are in publish/ the build is done
# and the only question that matters is whether the payload about to be
# archived is shippable.
#
# What gets bundled (the Xcode "Copy Frameworks"/"Copy Libraries" phases of
# the Blink target):
#
#     Distribution/Frameworks/libs               folder reference — ships whole
#     Distribution/Frameworks/Python.framework
#     Distribution/Frameworks/Sparkle.framework
#     Distribution/Resources/lib                 folder reference — ships whole
#
# Everything else under Distribution/ (libs-arm64, libs-x86_64, lib-arm64,
# lib-x86_64, lib-, *.orig, dated backups, Notary/, staging/) is scaffolding
# and is deliberately NOT audited: it never reaches the app, and including it
# only produces false failures.
#
# The audit itself is check-universal.sh — the same code 05-verify.sh runs
# after export. Here it runs on the staged payload (so a problem is caught
# before archiving + notarizing), there it runs on the finished .app.
#
# Usage:        ./01-check.sh
# Exit status:  0 = payload is shippable, non-zero = fix before archiving.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$SCRIPT_DIR/../../Distribution"
AUDIT="$SCRIPT_DIR/check-universal.sh"

if [ ! -x "$AUDIT" ]; then
    echo "error: required checker not found or not executable:" >&2
    echo "       $AUDIT" >&2
    exit 1
fi

# Exactly the paths the Xcode copy phases bundle.
BUNDLED="
$DIST/Frameworks/libs
$DIST/Frameworks/Python.framework
$DIST/Frameworks/Sparkle.framework
$DIST/Resources/lib
"

paths=""
missing=""
for p in $BUNDLED; do
    if [ -d "$p" ]; then
        paths="$paths $p"
    else
        missing="$missing $p"
    fi
done

if [ -n "$missing" ]; then
    echo "error: these bundled payload directories are missing:" >&2
    for p in $missing; do echo "       ${p#$DIST/}" >&2; done
    echo >&2
    echo "       Run the build_scripts steps first (05-copy-libraries.sh," >&2
    echo "       06-copy-python-packages.sh, 07-copy-python-framework.sh)." >&2
    exit 1
fi

echo "==> Payload check: everything Xcode will bundle into Blink.app"
echo
for p in $paths; do echo "    ${p#$DIST/}"; done
echo

CHECK_INDENT="    " "$AUDIT" $paths
rc=$?

echo
if [ "$rc" -eq 0 ]; then
    echo "==> OK — the payload is universal and slice-consistent."
    echo "    Proceed with ./02-archive.sh"
else
    echo "==> FAILED — fix the items above before archiving." >&2
    echo "    Single-arch: reinstall the offending MacPorts port +universal" >&2
    echo "    (build_scripts/02-install-c-deps.sh), then re-run" >&2
    echo "    04-install_sipsimple.sh and 05-copy-libraries.sh." >&2
    echo "    Note 05-copy-libraries.sh does NOT overwrite libs that are" >&2
    echo "    already in Frameworks/libs — delete the stale ones first." >&2
fi
exit "$rc"
