#!/bin/bash

# brew install create-dmg

if [ -f d,g/Blink.dmg ]; then rm -rf dmg/Blink.dmg; fi

rm -r dmg/staging/*

cp -a ../../Distribution/Notary/Blink.app dmg/staging/
cp ../../ReleaseNotes/ReleaseNotes.txt dmg/staging/
cp ../../LICENSE dmg/staging/LICENSE

cd dmg/staging
    ln -sf /Applications .
cd -

create-dmg --window-size 475 520 --icon "Blink.app" 0 165 \
--icon "Applications" 240 165 \
--icon "LICENSE" 0 365 \
--icon "ReleaseNotes.txt" 240 365 \
--volname "Blink SIP Client" \
--background dmg/background.png \
--icon-size 64 dmg/Blink.dmg dmg/staging

open dmg/Blink.dmg
