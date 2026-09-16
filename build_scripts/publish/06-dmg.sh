#!/bin/bash
#
# sudo port install create-dmg
#
# Layout reference -- background.png is drawn 1:1 in window points, anchored
# top-left, so these are background.png pixel coordinates:
#
#   white box      x  14..457   y  14..257
#   arrow          x 220..288   y ~155..190
#   rule           y ~457
#   "AG Projects"  y ~477
#
# create-dmg --icon coordinates are the CENTRE of the icon.

set -euo pipefail

cd "$(dirname "$0")"

WINDOW_W=475
WINDOW_H=520
ICON_SIZE=64

COL_L=130          # left column  -- left of the arrow
COL_R=378          # right column -- right of the arrow
ROW_TOP=172        # on the arrow's axis, inside the white box
ROW_BOTTOM=350     # below the box, above the rule

rm -f dmg/Blink.dmg
rm -f dmg/rw.*.dmg                  # leftovers from an aborted create-dmg run

# Recreate staging from scratch. A stale .DS_Store in here is baked into the
# image and Finder honours it over anything create-dmg sets, which is the
# classic cause of icons landing in the wrong place.
rm -rf dmg/staging
mkdir -p dmg/staging

cp -a ../../Distribution/Notary/Blink.app dmg/staging/
cp ../../ReleaseNotes/ReleaseNotes.txt    dmg/staging/
cp ../../LICENSE                          dmg/staging/LICENSE.txt

find dmg/staging -name .DS_Store -delete

create-dmg \
    --volname "Blink SIP Client" \
    --background dmg/background.png \
    --window-pos 200 120 \
    --window-size $WINDOW_W $WINDOW_H \
    --icon-size $ICON_SIZE \
    --icon "Blink.app"        $COL_L $ROW_TOP \
    --app-drop-link           $COL_R $ROW_TOP \
    --icon "LICENSE.txt"      $COL_L $ROW_BOTTOM \
    --icon "ReleaseNotes.txt" $COL_R $ROW_BOTTOM \
    dmg/Blink.dmg dmg/staging

open dmg/Blink.dmg
