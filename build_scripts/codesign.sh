#!/bin/sh
sos=`find ./Resources -name *.dylib`; for s in $sos; do codesign -f -o runtime --timestamp --entitlements Entitlements.plist -s "Developer ID Application" $s; done
sos=`find ./Resources -name *.so`; for s in $sos; do codesign -f -o runtime --timestamp --entitlements Entitlements.plist -s "Developer ID Application" $s; done
sos=`find ./Frameworks -name *.dylib`; for s in $sos; do codesign -f -o runtime --timestamp --entitlements Entitlements.plist -s "Developer ID Application" $s; done
sos=`find ./Frameworks -name *.so`; for s in $sos; do codesign -f -o runtime --timestamp --entitlements Entitlements.plist -s "Developer ID Application" $s; done
codesign -f -o runtime --timestamp --entitlements Entitlements.plist -s "Developer ID Application" Frameworks/Python.framework
