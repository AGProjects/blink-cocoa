#!/bin/bash
#
# debug-sparkle.sh — diagnose why Sparkle auto-update may not be offering /
# applying a new Blink release.
#
# Sparkle 1.x decides "is there an update?" by comparing the HOST app's
# CFBundleVersion against the appcast item's sparkle:version (SUStandard-
# VersionComparator). Blink uses the *build number* (CURRENT_PROJECT_VERSION)
# as CFBundleVersion and the *marketing string* (MARKETING_VERSION) as
# CFBundleShortVersionString, so the appcast MUST advertise:
#     sparkle:version            = build number   (gates the update)
#     sparkle:shortVersionString = marketing      (display only)
# This script surfaces all the moving parts and gives a verdict.
#
# Usage:
#     ./debug-sparkle.sh                       # checks against /Applications/Blink.app
#     ./debug-sparkle.sh /path/to/Blink.app    # check a specific installed app
#     ./debug-sparkle.sh --download            # also fetch+mount the served DMG and read its version
#     ./debug-sparkle.sh --log                 # stream Sparkle/Blink unified logs (blocking; Ctrl-C to stop)
#
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PROJECT="../../Blink.xcodeproj"
LOCAL_APPCAST="../../ReleaseNotes/BlinkAppcast.xml"
INFO_PLIST="../../Info.plist"
SCHEME="Blink"
PB=/usr/libexec/PlistBuddy

APP="/Applications/Blink.app"
DO_DOWNLOAD=0
DO_LOG=0
for a in "$@"; do
    case "$a" in
        --download) DO_DOWNLOAD=1 ;;
        --log)      DO_LOG=1 ;;
        -h|--help)  sed -n '2,22p' "$0"; exit 0 ;;
        -*)         echo "Unknown option: $a" >&2; exit 2 ;;
        *)          APP="$a" ;;
    esac
done

hr() { printf '%s\n' "------------------------------------------------------------"; }

read_setting() {  # $1 = build setting
    xcodebuild -project "$PROJECT" -scheme "$SCHEME" -configuration Release \
        -showBuildSettings 2>/dev/null \
        | awk -F' = ' -v k="$1" '$0 ~ "^[[:space:]]*" k " ="{gsub(/[[:space:]]/,"",$2); print $2; exit}'
}

# SUStandardVersionComparator-style compare. Prints: newer | older | same
# ("$1 relative to $2"). Good enough to mirror Sparkle's gating for the
# integer-build vs dotted-marketing cases that cause the regression.
vcmp() {
python3 - "$1" "$2" <<'PY'
import sys, re
def parts(v):
    out=[]
    for tok in re.findall(r'\d+|[A-Za-z]+', v or ""):
        out.append((0,int(tok)) if tok.isdigit() else (1,tok))
    return out
a,b=parts(sys.argv[1]),parts(sys.argv[2])
for x,y in zip(a,b):
    if x!=y:
        print("newer" if x>y else "older"); break
else:
    if len(a)!=len(b): print("newer" if len(a)>len(b) else "older")
    else: print("same")
PY
}

# ---------------------------------------------------------------------------
echo "Sparkle update diagnostics"
hr

# 1) What the Xcode project will ship -----------------------------------------
echo "[1] Xcode project ($SCHEME scheme) — source of truth"
if command -v xcodebuild >/dev/null 2>&1; then
    PROJ_MKT="$(read_setting MARKETING_VERSION)"
    PROJ_BUILD="$(read_setting CURRENT_PROJECT_VERSION)"
    echo "    MARKETING_VERSION        (CFBundleShortVersionString) : ${PROJ_MKT:-?}"
    echo "    CURRENT_PROJECT_VERSION  (CFBundleVersion)            : ${PROJ_BUILD:-?}"
else
    echo "    xcodebuild not found — skipping"
fi
echo "    Feed URL (Info.plist SUFeedURL): $($PB -c 'Print :SUFeedURL' "$INFO_PLIST" 2>/dev/null || echo '?')"
hr

# 2) Installed app -----------------------------------------------------------
echo "[2] Installed app: $APP"
if [ -d "$APP" ]; then
    APP_SHORT="$($PB -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null)"
    APP_BUILD="$($PB -c 'Print :CFBundleVersion'            "$APP/Contents/Info.plist" 2>/dev/null)"
    SPK="$($PB -c 'Print :CFBundleShortVersionString' "$APP/Contents/Frameworks/Sparkle.framework/Resources/Info.plist" 2>/dev/null)"
    echo "    CFBundleShortVersionString (display) : ${APP_SHORT:-?}"
    echo "    CFBundleVersion  (gates updates)     : ${APP_BUILD:-?}"
    echo "    Sparkle.framework version            : ${SPK:-?}"
else
    echo "    not found — pass the path: ./debug-sparkle.sh /path/to/Blink.app"
    APP_BUILD=""
fi
hr

# 3) Appcast (live feed + local file) ----------------------------------------
parse_top() {  # $1 = file ; sets globals TOP_SVER TOP_SHORT TOP_URL TOP_LEN
    TOP_SVER="$(grep -m1 'sparkle:version='            "$1" | sed -E 's/.*sparkle:version="([^"]+)".*/\1/')"
    TOP_SHORT="$(grep -m1 'sparkle:shortVersionString=' "$1" | sed -E 's/.*sparkle:shortVersionString="([^"]+)".*/\1/')"
    TOP_URL="$(grep -m1 'enclosure url='               "$1" | sed -E 's/.*enclosure url="([^"]+)".*/\1/')"
    TOP_LEN="$(grep -m1 'length='                      "$1" | sed -E 's/.*length="([^"]+)".*/\1/')"
}

FEED="$($PB -c 'Print :SUFeedURL' "$INFO_PLIST" 2>/dev/null)"
LIVE="$(mktemp)"
echo "[3] Appcast"
if [ -n "$FEED" ] && curl -fsSL "$FEED" -o "$LIVE" 2>/dev/null; then
    parse_top "$LIVE"
    echo "    LIVE  ($FEED)"
    echo "      top sparkle:version=${TOP_SVER:-<none>}  shortVersionString=${TOP_SHORT:-<none>}"
    LIVE_SVER="$TOP_SVER"
else
    echo "    LIVE: could not fetch $FEED"
    LIVE_SVER=""
fi
if [ -f "$LOCAL_APPCAST" ]; then
    parse_top "$LOCAL_APPCAST"
    echo "    LOCAL ($LOCAL_APPCAST)"
    echo "      top sparkle:version=${TOP_SVER:-<none>}  shortVersionString=${TOP_SHORT:-<none>}"
    echo "      enclosure url=${TOP_URL:-<none>}"
    [ -z "$TOP_SHORT" ] && echo "      WARNING: top entry has NO sparkle:shortVersionString (display will fall back to sparkle:version)."
    case "$TOP_SVER" in
        *.*) echo "      WARNING: top sparkle:version looks like a marketing string ('$TOP_SVER'), not a build number." ;;
    esac
fi
rm -f "$LIVE"
hr

# 4) Download URL headers ----------------------------------------------------
echo "[4] Download URL (what Sparkle fetches)"
if [ -n "${TOP_URL:-}" ]; then
    echo "    curl -sIL $TOP_URL"
    curl -sIL "$TOP_URL" 2>/dev/null | grep -iE '^HTTP/|^location:|^content-type:|^content-length:|^content-disposition:' \
        | sed 's/^/      /'
    echo "    NB: the URL has no .dmg extension, so Sparkle relies on Content-Disposition"
    echo "        (or Content-Type) to recognise/unpack the DMG."
else
    echo "    no enclosure URL parsed — skipping"
fi
hr

# 5) Verdict -----------------------------------------------------------------
echo "[5] Verdict"
CMP_SVER="${LIVE_SVER:-${TOP_SVER:-}}"
if [ -n "$CMP_SVER" ] && [ -n "${APP_BUILD:-}" ]; then
    rel="$(vcmp "$CMP_SVER" "$APP_BUILD")"
    echo "    appcast sparkle:version ($CMP_SVER) vs installed CFBundleVersion ($APP_BUILD): appcast is $rel"
    case "$rel" in
        newer) echo "    => Sparkle WILL offer the update. (If it still fails, it's download/unpack — see [4] and --log.)" ;;
        same)  echo "    => Sparkle sees no update (already current build)." ;;
        older) echo "    => Sparkle will NOT offer it: the installed build outranks the appcast's sparkle:version."
               echo "       This is the classic build-number/marketing mismatch. Ensure sparkle:version is the"
               echo "       BUILD number and that it increases each release (see 07-version-bump.sh)." ;;
    esac
else
    echo "    not enough info (need both appcast sparkle:version and installed CFBundleVersion)."
fi
hr

# 6) Optional: inspect the actually-served DMG -------------------------------
if [ "$DO_DOWNLOAD" = "1" ] && [ -n "${TOP_URL:-}" ]; then
    echo "[6] Downloading + inspecting the served DMG ..."
    dmg="$(mktemp -d)/blink.dmg"
    if curl -fsSL "$TOP_URL" -o "$dmg" 2>/dev/null; then
        echo "    size: $(stat -f%z "$dmg" 2>/dev/null) bytes  (appcast length=${TOP_LEN:-<none>})"
        mnt="$(mktemp -d)"
        if hdiutil attach "$dmg" -nobrowse -quiet -mountpoint "$mnt" 2>/dev/null; then
            sapp="$(/usr/bin/find "$mnt" -maxdepth 1 -name '*.app' | head -1)"
            if [ -n "$sapp" ]; then
                echo "    served app: $(basename "$sapp")"
                echo "      CFBundleShortVersionString: $($PB -c 'Print :CFBundleShortVersionString' "$sapp/Contents/Info.plist" 2>/dev/null)"
                echo "      CFBundleVersion           : $($PB -c 'Print :CFBundleVersion'            "$sapp/Contents/Info.plist" 2>/dev/null)"
                echo "      (should match appcast sparkle:version=${TOP_SVER:-?} / shortVersionString=${TOP_SHORT:-?})"
            else
                echo "    no .app found inside the DMG (server may be serving HTML/an error page)."
            fi
            hdiutil detach "$mnt" -quiet 2>/dev/null
        else
            echo "    could not mount the downloaded file — not a valid DMG? (check [4] headers)"
        fi
    else
        echo "    download failed: $TOP_URL"
    fi
    hr
fi

# 7) Optional: live Sparkle logs ---------------------------------------------
if [ "$DO_LOG" = "1" ]; then
    echo "[7] Streaming logs (Ctrl-C to stop). Now trigger 'Check for Updates…' in Blink."
    echo "    Look for: 'is not newer', unarchiver, signature, or download errors."
    exec log stream --info --debug --predicate 'process == "Blink" OR senderImagePath CONTAINS "Sparkle"'
fi

echo "Done. Re-run with --download to inspect the served DMG, or --log to watch Sparkle decide."
