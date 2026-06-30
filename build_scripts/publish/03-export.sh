#!/bin/bash
if [ -d ../../Distribution/Notary/Blink.app ]; then
    rm -r ../../Distribution/Notary/Blink.app
fi

xcodebuild -verbose -exportArchive -archivePath ../../Distribution/Notary/Blink.xcarchive -exportPath ../../Distribution/Notary/ -exportOptionsPlist ExportOptions.plist
