#!/bin/bash

cd ../../Distribution
d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

if [ -f Blink.dmg ]; then rm -rf Blink.dmg; fi

rm -r staging/Blink.app
cp -a Notary/Blink.app staging/
spctl -a -t exec -vvv staging/Blink.app

# Copy Release Notes
cp ../ReleaseNotes/ReleaseNotes.txt staging/
cp ../LICENSE staging/License.txt

# Make dmg
hdiutil makehybrid -hfs -hfs-volume-name Blink -hfs-openfolder staging staging -o tmp.dmg
hdiutil convert -format UDBZ tmp.dmg -o Blink.dmg
rm tmp.dmg

echo "DMG size:"
du -sk Blink.dmg

id="Developer ID Application: AG Projects"
codesign -f -s "$id" Blink.dmg

