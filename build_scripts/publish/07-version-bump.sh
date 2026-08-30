#!/bin/bash
#
# 07-version-bump.sh — stamp ReleaseNotes/BlinkAppcast.xml with a release entry.
#
# Sparkle 1.x gates updates on the host's CFBundleVersion compared against the
# appcast item's sparkle:version. Blink's CFBundleVersion is the *build number*
# (CURRENT_PROJECT_VERSION), while the human-readable version is the marketing
# string (MARKETING_VERSION). So the appcast entry must carry:
#     sparkle:version            = build number   (CURRENT_PROJECT_VERSION)  <- gates updates
#     sparkle:shortVersionString = marketing      (MARKETING_VERSION)        <- shown to user
# Mixing these up (e.g. putting the marketing version in sparkle:version) breaks
# auto-update once the build number grows past the marketing major (see the
# Sparkle regression notes). Both values are read from the Xcode project, which
# is the single source of truth.
#
# Usage:
#     ./07-version-bump.sh                 # read marketing + build from the Blink scheme
#     ./07-version-bump.sh 9.5.0           # override the marketing/display version
#     ./07-version-bump.sh --build 938     # override the build number (sparkle:version)
#     ./07-version-bump.sh --force         # add the entry even if it duplicates the top
#
# IMPORTANT: the build number MUST increase every release, otherwise Sparkle
# will not offer the update. This script refuses a non-increasing build number
# unless --force is given.
#
# The entry is cloned from the previous one, so anything stale in that entry
# propagates forever. Two fields are therefore rewritten explicitly rather
# than inherited:
#
#   enclosure url  -- must end in .dmg. Sparkle 1.x names the downloaded file
#                     from the server's suggested filename, and picks an
#                     unarchiver by that name's EXTENSION. An enclosure
#                     pointing at a redirector script (download-beta.phtml)
#                     downloads fine and then fails with "An error occurred
#                     while extracting the archive", because .phtml matches no
#                     unarchiver. Point it at the file itself.
#   minimumSystemVersion -- the OS Blink is actually built for, from
#                     blink_min_os.sh. An appcast claiming a newer floor than
#                     the app has silently withholds every update from the
#                     users on the older OS.
#
# Run it after 06-dmg.sh and before 08-upload.sh (which rsyncs the appcast).
#
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PROJECT="../../Blink.xcodeproj"
APPCAST="../../ReleaseNotes/BlinkAppcast.xml"
CHANGELOG="../../ReleaseNotes/changelog-classic.html"
SCHEME="Blink"
DMG="dmg/Blink.dmg"

# Where 08-upload.sh puts the DMG, as a URL. It has to be the .dmg itself:
# see the note above about how Sparkle chooses an unarchiver.
DMG_URL="${BLINK_DMG_URL:-https://download.ag-projects.com/Blink/MacOSX/Blink.dmg}"

# The floor the app is really built for.
MIN_OS="$(BLINK_MIN_OS="${BLINK_MIN_OS:-}" . ../blink_min_os.sh >/dev/null 2>&1; echo "${BLINK_MIN_OS:-13.0}")"

FORCE=0
EXPLICIT_MKT=""
EXPLICIT_BUILD=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force)      FORCE=1 ;;
        --build)      shift; EXPLICIT_BUILD="${1:-}" ;;
        --build=*)    EXPLICIT_BUILD="${1#*=}" ;;
        -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
        -*)           echo "Unknown option: $1" >&2; exit 2 ;;
        *)            EXPLICIT_MKT="$1" ;;
    esac
    shift
done

[ -f "$APPCAST" ] || { echo "Appcast not found: $APPCAST" >&2; exit 1; }

is_dotted() { [[ "$1" =~ ^[0-9]+(\.[0-9]+)*$ ]]; }
is_int()    { [[ "$1" =~ ^[0-9]+$ ]]; }

read_setting() {  # $1 = build setting name
    xcodebuild -project "$PROJECT" -scheme "$SCHEME" -configuration Release \
        -showBuildSettings 2>/dev/null \
        | awk -F' = ' -v k="$1" '$0 ~ "^[[:space:]]*" k " ="{gsub(/[[:space:]]/,"",$2); print $2; exit}'
}

# --- marketing (display) version -------------------------------------------
if [ -n "$EXPLICIT_MKT" ]; then
    MKT="$EXPLICIT_MKT"
    is_dotted "$MKT" || { echo "Invalid marketing version: '$MKT'" >&2; exit 1; }
else
    command -v xcodebuild >/dev/null 2>&1 || {
        echo "xcodebuild not found — cannot read the project version." >&2
        echo "Pass versions explicitly, e.g. ./07-version-bump.sh 9.5.0 --build 938" >&2
        exit 1; }
    echo "Reading MARKETING_VERSION / CURRENT_PROJECT_VERSION from the '$SCHEME' scheme ..."
    MKT="$(read_setting MARKETING_VERSION)"
    [ -n "$MKT" ] || { echo "Could not read MARKETING_VERSION from $PROJECT." >&2; exit 1; }
fi

# --- build number (sparkle:version, the field Sparkle compares) -------------
if [ -n "$EXPLICIT_BUILD" ]; then
    BUILD="$EXPLICIT_BUILD"
else
    BUILD="$(read_setting CURRENT_PROJECT_VERSION 2>/dev/null)"
    [ -n "$BUILD" ] || { echo "Could not read CURRENT_PROJECT_VERSION — pass --build N." >&2; exit 1; }
fi
is_int "$BUILD" || { echo "Build number must be an integer (got '$BUILD')." >&2; exit 1; }

echo "  marketing (shortVersionString): $MKT"
echo "  build     (sparkle:version):    $BUILD"

# --- guards ----------------------------------------------------------------
# Top entry's current sparkle:version = previous build number.
TOPBUILD="$(grep -m1 'sparkle:version=' "$APPCAST" | sed -E 's/.*sparkle:version="([^"]+)".*/\1/')"

if [ "$TOPBUILD" = "$BUILD" ] && [ "$FORCE" != "1" ]; then
    echo "Appcast top entry already has build $BUILD — nothing to do."
    echo "(Bump CURRENT_PROJECT_VERSION in Xcode, or use --build N / --force.)"
    exit 0
fi

# Refuse a non-increasing build number (the exact failure that breaks Sparkle):
# only meaningful when the previous entry is itself a plain integer build number.
if is_int "$TOPBUILD" && [ "$BUILD" -le "$TOPBUILD" ] && [ "$FORCE" != "1" ]; then
    echo "ERROR: new build $BUILD is not greater than the current top build $TOPBUILD." >&2
    echo "Sparkle would NOT offer this update. Bump CURRENT_PROJECT_VERSION (or --force)." >&2
    exit 1
fi

NEWDATE="$(date "+%a %b %e %H:%M:%S %Z %Y")"

# --- prepend a cloned <item> with the new versions + date ------------------
DMG_LENGTH=""
if [ -f "$DMG" ]; then
    DMG_LENGTH="$(stat -f%z "$DMG" 2>/dev/null || echo '')"
fi

APPCAST="$APPCAST" MKT="$MKT" BUILD="$BUILD" NEWDATE="$NEWDATE" \
DMG_URL="$DMG_URL" MIN_OS="$MIN_OS" DMG_LENGTH="$DMG_LENGTH" python3 - <<'PY'
import os, re, sys
path = os.environ["APPCAST"]; mkt = os.environ["MKT"]; build = os.environ["BUILD"]; date = os.environ["NEWDATE"]
dmg_url = os.environ["DMG_URL"]; min_os = os.environ["MIN_OS"]; length = os.environ.get("DMG_LENGTH", "")
s = open(path, encoding="utf-8").read()
m = re.search(r'[ \t]*<item>.*?</item>\n', s, re.S)
if not m:
    sys.exit("No <item> element found in appcast.")
block = m.group(0)
nb = block
# Title shows the marketing version.
nb = re.sub(r'<title>.*?</title>', '<title>Version %s</title>' % mkt, nb, count=1, flags=re.S)
# sparkle:version = build number (what Sparkle compares).
nb = re.sub(r'sparkle:version="[^"]*"', 'sparkle:version="%s"' % build, nb, count=1)
# sparkle:shortVersionString = marketing version: replace if present, else add
# it right after the sparkle:version line (preserving indentation).
if 'sparkle:shortVersionString=' in nb:
    nb = re.sub(r'sparkle:shortVersionString="[^"]*"',
                'sparkle:shortVersionString="%s"' % mkt, nb, count=1)
else:
    nb = re.sub(r'(^([ \t]*)sparkle:version="[^"]*"\n)',
                r'\1\2sparkle:shortVersionString="%s"\n' % mkt, nb, count=1, flags=re.M)
nb = re.sub(r'<pubDate>.*?</pubDate>', '<pubDate>%s</pubDate>' % date, nb, count=1, flags=re.S)
# Stated rather than inherited -- see the note at the top of this script.
nb = re.sub(r'(<enclosure\s+url=")[^"]*(")', r'\g<1>%s\g<2>' % dmg_url, nb, count=1)
nb = re.sub(r'<sparkle:minimumSystemVersion>[^<]*</sparkle:minimumSystemVersion>',
            '<sparkle:minimumSystemVersion>%s</sparkle:minimumSystemVersion>' % min_os,
            nb, count=1)
if length:
    if 'length=' in nb:
        nb = re.sub(r'length="[^"]*"', 'length="%s"' % length, nb, count=1)
    else:
        nb = re.sub(r'(^([ \t]*)sparkle:version="[^"]*"\n)',
                    r'\1\2length="%s"\n' % length, nb, count=1, flags=re.M)
if nb == block:
    sys.exit("Refusing to write: template substitution changed nothing.")
s = s[:m.start()] + nb + s[m.start():]
open(path, "w", encoding="utf-8").write(s)
PY

if [ $? -ne 0 ]; then
    echo "Failed to update appcast." >&2
    exit 1
fi

echo "Added 'Version $MKT' (build $BUILD, $NEWDATE) to $APPCAST"

# --- also prepend a human-readable entry to the classic changelog ----------
# Mirrors the appcast: <h2>Version MKT</h2> with the release date and a
# default "Bug fixes" bullet. The marketing version comes from Xcode (MKT);
# the date is today, formatted like "June 30th, 2026" to match existing entries.
if [ -f "$CHANGELOG" ]; then
    CHANGELOG="$CHANGELOG" MKT="$MKT" FORCE="$FORCE" python3 - <<'PY'
import os, re, sys, datetime
path = os.environ["CHANGELOG"]; mkt = os.environ["MKT"]; force = os.environ["FORCE"]
s = open(path, encoding="utf-8").read()

# Skip if the top entry is already this marketing version (unless --force).
top = re.search(r'<h2>Version\s+([^<]+)</h2>', s)
if top and top.group(1).strip() == mkt and force != "1":
    print("Changelog top entry already has Version %s — skipping." % mkt)
    sys.exit(0)

# Date like "June 30th, 2026".
today = datetime.date.today()
d = today.day
suffix = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
date_str = "%s %d%s, %d" % (today.strftime("%B"), d, suffix, today.year)

entry = (
    "<h2>Version %s</h2>\n\n"
    "<p>%s\n\n"
    "<ul>\n"
    "<li>Bug fixes\n"
    "</ul>\n\n\n"
) % (mkt, date_str)

# Insert immediately before the first existing version entry.
m = re.search(r'<h2>Version\s', s)
if not m:
    sys.exit("No existing <h2>Version ...</h2> entry found in changelog.")
s = s[:m.start()] + entry + s[m.start():]
open(path, "w", encoding="utf-8").write(s)
print("Added 'Version %s' (%s) to %s" % (mkt, date_str, path))
PY
    [ $? -eq 0 ] || { echo "Failed to update changelog." >&2; exit 1; }
else
    echo "Changelog not found: $CHANGELOG (skipping)" >&2
fi

echo "Next: ./08-upload.sh"
