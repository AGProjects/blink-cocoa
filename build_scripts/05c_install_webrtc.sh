#!/bin/bash
#
# Install Google's WebRTC.framework (libwebrtc, Objective-C API) into
# ../Distribution/Frameworks, for the SylkServer video conferences (sylkRTC
# to the webrtcgateway). Blink loads it at run time with objc.loadBundle; no
# target links against it. Xcode's "Copy Frameworks" phase of Blink and
# Blink Pro copies it into the .app (Code Sign On Copy, Remove Headers On
# Copy), as it does for Sparkle.framework.
#
# Source: the prebuilt xcframework of https://github.com/stasel/WebRTC
# (BSD licence, the same build Swift Package Manager fetches). Only the
# macos-x86_64_arm64 slice is used: universal, minos 13.0, linked against
# system frameworks only. The zip is pinned by version and SHA-256, so every
# Mac installs the same bytes; a mismatch is a hard error.
#
# Steps:
#   1. download the pinned zip into a cache (once) and verify its SHA-256
#   2. copy WebRTC.framework from the macOS slice into Distribution/Frameworks
#   3. drop Headers, Modules and PrivateHeaders (not needed at run time)
#   4. set the install name to @executable_path/../Frameworks/... (was @rpath)
#   5. check: arm64 + x86_64, minos <= BLINK_MIN_OS, no non-system dependency
#   6. sign (hardened runtime, timestamp)
#   7. if a Python with PyObjC is at hand, load the bundle and look up
#      RTCPeerConnectionFactory
#
# Run from build_scripts/, after 05-copy-libraries.sh. Re-running is safe:
# the framework is always replaced, the download is reused from the cache.
#
# Usage:
#   ./05c_install_webrtc.sh            install
#   ./05c_install_webrtc.sh --force    download again, ignoring the cache
#
# Knobs (env vars):
#   WEBRTC_VERSION   release tag            (default: 155.0.0)
#   WEBRTC_MILESTONE asset milestone        (default: M155)
#   WEBRTC_SHA256    SHA-256 of the zip     (default: the M155 checksum)
#   WEBRTC_CACHE     download cache         (default: ~/Library/Caches/blink-build/webrtc)
#   SIGN_IDENTITY    codesign identity      (default: "Developer ID Application";
#                                            "-" signs ad hoc for a local test)
#
# To move to a newer release, take the version and checksum from
# https://github.com/stasel/WebRTC/releases (the checksum is the one in that
# release's Package.swift) and change the three defaults below together.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=blink_min_os.sh
source "$SCRIPT_DIR/blink_min_os.sh"

WEBRTC_VERSION="${WEBRTC_VERSION:-155.0.0}"
WEBRTC_MILESTONE="${WEBRTC_MILESTONE:-M155}"
WEBRTC_SHA256="${WEBRTC_SHA256:-cb9adacf2d6bbf598d1d9e3e6c16d8fb97ca987b5b697746891828f9dc068ffe}"
WEBRTC_CACHE="${WEBRTC_CACHE:-$HOME/Library/Caches/blink-build/webrtc}"
SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application}"

ZIP_NAME="WebRTC-$WEBRTC_MILESTONE.xcframework.zip"
URL="https://github.com/stasel/WebRTC/releases/download/$WEBRTC_VERSION/$ZIP_NAME"
SLICE="WebRTC.xcframework/macos-x86_64_arm64"

FRAMEWORKS_DIR="$(cd ../Distribution 2>/dev/null && pwd)/Frameworks" || {
    echo "error: ../Distribution not found — run the earlier build steps first." >&2
    exit 1
}
DEST="$FRAMEWORKS_DIR/WebRTC.framework"
BINARY_REL="Versions/A/WebRTC"
INSTALL_NAME="@executable_path/../Frameworks/WebRTC.framework/$BINARY_REL"

FORCE=0
case "${1:-}" in
    --force) FORCE=1 ;;
    "") ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac

die() { echo "error: $*" >&2; exit 1; }

for tool in curl shasum unzip lipo otool install_name_tool codesign ditto; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' not found (xcode-select --install)"
done

minos_of() {
    otool -arch "$2" -l "$1" 2>/dev/null | awk '
        $1 == "cmd" && $2 == "LC_BUILD_VERSION"      { want = "minos";   next }
        $1 == "cmd" && $2 == "LC_VERSION_MIN_MACOSX" { want = "version"; next }
        want != "" && $1 == want                     { print $2; exit }'
}

# ---------------------------------------------------------------------------
# 1. Download (cached) and verify.
# ---------------------------------------------------------------------------
mkdir -p "$WEBRTC_CACHE"
ZIP="$WEBRTC_CACHE/$ZIP_NAME"

if [ "$FORCE" -eq 1 ]; then
    rm -f "$ZIP"
fi

if [ -f "$ZIP" ] && [ "$(shasum -a 256 "$ZIP" | awk '{print $1}')" != "$WEBRTC_SHA256" ]; then
    echo "Cached $ZIP_NAME does not match the pinned checksum; downloading again."
    rm -f "$ZIP"
fi

if [ ! -f "$ZIP" ]; then
    echo "Downloading $URL ..."
    curl -fL --retry 3 -o "$ZIP.part" "$URL" || { rm -f "$ZIP.part"; die "download failed"; }
    mv "$ZIP.part" "$ZIP"
fi

actual="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
if [ "$actual" != "$WEBRTC_SHA256" ]; then
    rm -f "$ZIP"
    die "SHA-256 mismatch for $ZIP_NAME
  expected $WEBRTC_SHA256
  got      $actual"
fi
echo "WebRTC $WEBRTC_MILESTONE ($WEBRTC_VERSION): checksum OK."

# ---------------------------------------------------------------------------
# 2. Unpack the macOS slice and copy the framework.
# ---------------------------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/blink-webrtc.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

unzip -q "$ZIP" "$SLICE/*" "WebRTC.xcframework/LICENSE" -d "$WORK" \
    || die "unzip failed (no $SLICE in $ZIP_NAME?)"
[ -f "$WORK/$SLICE/WebRTC.framework/$BINARY_REL" ] || die "no WebRTC binary in $SLICE"

rm -rf "$DEST"
mkdir -p "$FRAMEWORKS_DIR"
# ditto keeps the Versions/Current symlinks a framework needs.
ditto "$WORK/$SLICE/WebRTC.framework" "$DEST"

# ---------------------------------------------------------------------------
# 3. Strip what is only for compiling against it; keep the licence.
# ---------------------------------------------------------------------------
for d in Headers Modules PrivateHeaders; do
    rm -rf "$DEST/$d" "$DEST/Versions/A/$d"
done
cp "$WORK/WebRTC.xcframework/LICENSE" "$DEST/Versions/A/Resources/LICENSE"

# The framework's Info.plist says 1.0 whatever the release: record the release
# (sealed by the signature below), for the Activity log.
PLIST="$DEST/Versions/A/Resources/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $WEBRTC_VERSION" "$PLIST" \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $WEBRTC_VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :WebRTCMilestone" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :WebRTCMilestone string $WEBRTC_MILESTONE" "$PLIST"

# ---------------------------------------------------------------------------
# 4. Install name: @rpath -> the app's Frameworks folder, as for Sparkle.
#    (Loading by path does not need it; anything that links against the
#    framework later would otherwise depend on an LC_RPATH the app lacks.)
# ---------------------------------------------------------------------------
BIN="$DEST/$BINARY_REL"
chmod u+w "$BIN"
codesign --remove-signature "$BIN" 2>/dev/null || true
install_name_tool -id "$INSTALL_NAME" "$BIN"
# Drop any LC_RPATH it carries (none in M155, but a later build may add one).
for rp in $(otool -l "$BIN" | awk '$1 == "cmd" && $2 == "LC_RPATH" { getline; getline; print $2 }' | sort -u); do
    install_name_tool -delete_rpath "$rp" "$BIN" || die "could not delete LC_RPATH $rp"
done

# ---------------------------------------------------------------------------
# 5. Checks.
# ---------------------------------------------------------------------------
failed=0
archs="$(lipo -archs "$BIN")"
for want in x86_64 arm64; do
    case " $archs " in
        *" $want "*) ;;
        *) echo "  NOT UNIVERSAL: $want missing (has: $archs)"; failed=1 ;;
    esac
done

min_allowed="$(ver_num "$BLINK_MIN_OS")"
for arch in $archs; do
    v="$(minos_of "$BIN" "$arch")"
    if [ -n "$v" ] && [ "$(ver_num "$v")" -gt "$min_allowed" ]; then
        echo "  BUILT TOO NEW: $arch minos $v, want $BLINK_MIN_OS or older"
        failed=1
    fi
    echo "  $arch: minos ${v:-<none>}"
done

# Every dependency must come with macOS; a /opt/local or @rpath one would
# not exist on a customer's Mac.
# (otool -L prints a "<file> (architecture ...):" header per slice: skipped.)
others="$(otool -L "$BIN" | awk '!/:$/ {print $1}' | sort -u \
          | grep -v -e '^/System/Library/' -e '^/usr/lib/' -e "^$INSTALL_NAME\$" || true)"
if [ -n "$others" ]; then
    echo "  NON-SYSTEM DEPENDENCIES:"
    printf '    %s\n' $others
    failed=1
fi

id="$(otool -D "$BIN" | tail -n 1)"
[ "$id" = "$INSTALL_NAME" ] || { echo "  install name is $id, want $INSTALL_NAME"; failed=1; }

[ "$failed" -eq 0 ] || die "WebRTC.framework failed the checks above"
echo "  install name: $id"

# ---------------------------------------------------------------------------
# 6. Sign (the binary first, then the bundle that seals its resources).
# ---------------------------------------------------------------------------
codesign -f -o runtime --timestamp -s "$SIGN_IDENTITY" "$BIN"
codesign -f -o runtime --timestamp -s "$SIGN_IDENTITY" "$DEST"
codesign --verify --strict --verbose=1 "$DEST" || die "codesign verification failed"

# ---------------------------------------------------------------------------
# 7. Optional smoke test with PyObjC.
# ---------------------------------------------------------------------------
PY=""
for candidate in python3 "$FRAMEWORKS_DIR/Python.framework/Versions/Current/bin/python3"; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import objc' 2>/dev/null; then
        PY="$candidate"; break
    fi
done
if [ -n "$PY" ]; then
    "$PY" - "$DEST" <<'EOF' || die "PyObjC could not load WebRTC.framework"
import sys, objc
ns = {}
objc.loadBundle('WebRTC', ns, bundle_path=sys.argv[1])
cls = objc.lookUpClass('RTCPeerConnectionFactory')
print('  PyObjC: loaded, %s found' % cls.__name__)
EOF
else
    echo "  PyObjC smoke test skipped (no python3 with objc in PATH)"
fi

version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$DEST/Versions/A/Resources/Info.plist" 2>/dev/null || echo "$WEBRTC_VERSION")"
size="$(du -sh "$DEST" | awk '{print $1}')"
echo
echo "WebRTC.framework $version installed at $DEST ($size), signed with \"$SIGN_IDENTITY\"."
