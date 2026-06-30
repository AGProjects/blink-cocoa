#!/bin/bash
rm -r ../../Distribution/Notary/Blink.xcarchive
xcodebuild archive -project ../../Blink.xcodeproj -scheme Blink -archivePath ../../Distribution/Notary/Blink.xcarchive -configuration Release
