#!/bin/sh
sos=`find ./Frameworks/Python.Framework/ -name *.dylib`; for s in $sos; do codesign -f -o runtime --timestamp -s "Developer ID Application" $s; done
sos=`find ./Frameworks/Python.Framework/ -name *.so`; for s in $sos; do codesign -f -o runtime --timestamp -s "Developer ID Application" $s; done
codesign -f -o runtime --timestamp -s "Developer ID Application" Frameworks/Python.framework
