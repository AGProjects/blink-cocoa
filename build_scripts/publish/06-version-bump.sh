#!/bin/bash
#
# 06-version-bump.sh — stamp ReleaseNotes/BlinkAppcast.xml with a release entry.
#
# The version is the single source of truth in the Xcode project: the
# MARKETING_VERSION of the Blink target. By default this script reads that
# version *as-is* and prepends a matching <item> to the appcast, dated now.
# The new <item> is cloned from the current top entry, so minimumSystemVersion,
# releaseNotesLink and the enclosure url/type are preserved automatically.
#
# Usage:
#     ./06-version-bump.sh            # use the Xcode Blink MARKETING_VERSION as-is
#     ./06-version-bump.sh 9.5.0      # use an explicit version instead
#     ./06-version-bump.sh --bump     # increment the LAST number of the Xcode version
#     ./06-version-bump.sh --force    # add the entry even if it duplicates the top one
#
# Run it after 05-dmg.sh and before 07-upload.sh (which rsyncs the appcast).
#
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PROJECT="../../Blink.xcodeproj"
APPCAST="../../ReleaseNotes/BlinkAppcast.xml"
SCHEME="Blink"

FORCE=0
BUMP=0
EXPLICIT=""
for a in "$@"; do
    case "$a" in
        --force)    FORCE=1 ;;
        --bump|-b)  BUMP=1 ;;
        -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
        -*)         echo "Unknown option: $a" >&2; exit 2 ;;
        *)          EXPLICIT="$a" ;;
    esac
done

[ -f "$APPCAST" ] || { echo "Appcast not found: $APPCAST" >&2; exit 1; }

valid() { [[ "$1" =~ ^[0-9]+(\.[0-9]+)*$ ]]; }

# --- determine the version -------------------------------------------------
if [ -n "$EXPLICIT" ]; then
    NEWVER="$EXPLICIT"
    valid "$NEWVER" || { echo "Invalid version: '$NEWVER'" >&2; exit 1; }
    echo "Using explicit version: $NEWVER"
else
    command -v xcodebuild >/dev/null 2>&1 || {
        echo "xcodebuild not found — cannot read the project version." >&2
        echo "Pass a version explicitly, e.g. ./06-version-bump.sh 9.5.0" >&2
        exit 1; }
    echo "Reading MARKETING_VERSION from the '$SCHEME' scheme ..."
    XVER="$(xcodebuild -project "$PROJECT" -scheme "$SCHEME" -configuration Release \
            -showBuildSettings 2>/dev/null \
            | awk -F' = ' '/^[[:space:]]*MARKETING_VERSION =/{gsub(/[[:space:]]/,"",$2); print $2; exit}')"
    [ -n "$XVER" ] || { echo "Could not read MARKETING_VERSION from $PROJECT." >&2; exit 1; }
    valid "$XVER" || { echo "Project MARKETING_VERSION is not numeric: '$XVER'" >&2; exit 1; }
    if [ "$BUMP" = "1" ]; then
        NEWVER="${XVER%.*}.$(( ${XVER##*.} + 1 ))"
        echo "  project version: $XVER  ->  bumped: $NEWVER"
    else
        NEWVER="$XVER"
        echo "  project version: $XVER (used as-is)"
    fi
fi

# --- dedup guard -----------------------------------------------------------
TOPVER="$(grep -m1 'sparkle:version=' "$APPCAST" | sed -E 's/.*sparkle:version="([^"]+)".*/\1/')"
if [ "$TOPVER" = "$NEWVER" ] && [ "$FORCE" != "1" ]; then
    echo "Appcast already has Version $NEWVER at the top — nothing to do."
    echo "(Use --force to add it anyway, or --bump / an explicit version.)"
    exit 0
fi

NEWDATE="$(date "+%a %b %e %H:%M:%S %Z %Y")"

# --- prepend a cloned <item> with the new version + date -------------------
APPCAST="$APPCAST" NEWVER="$NEWVER" NEWDATE="$NEWDATE" python3 - <<'PY'
import os, re, sys
path = os.environ["APPCAST"]; ver = os.environ["NEWVER"]; date = os.environ["NEWDATE"]
s = open(path, encoding="utf-8").read()
# Grab the first (newest) <item>...</item> block, with its leading indent.
m = re.search(r'[ \t]*<item>.*?</item>\n', s, re.S)
if not m:
    sys.exit("No <item> element found in appcast.")
block = m.group(0)
nb = block
nb = re.sub(r'<title>.*?</title>', '<title>Version %s</title>' % ver, nb, count=1, flags=re.S)
nb = re.sub(r'sparkle:version="[^"]*"', 'sparkle:version="%s"' % ver, nb, count=1)
nb = re.sub(r'<pubDate>.*?</pubDate>', '<pubDate>%s</pubDate>' % date, nb, count=1, flags=re.S)
if nb == block:
    sys.exit("Refusing to write: template substitution changed nothing.")
s = s[:m.start()] + nb + s[m.start():]
open(path, "w", encoding="utf-8").write(s)
PY

if [ $? -ne 0 ]; then
    echo "Failed to update appcast." >&2
    exit 1
fi

echo "Added 'Version $NEWVER' ($NEWDATE) to $APPCAST"
echo "Next: ./07-upload.sh"
