#!/bin/sh
cd ../Distribution
d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

sos=`find ./Resources -name *.dylib`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Resources -name *.so`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Frameworks -name *.dylib`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Frameworks -name *.so`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
codesign -f -o runtime --timestamp  -s "Developer ID Application" Frameworks/Python.framework
