#!/bin/sh
id="Developer ID Application: AG Projects"
sos=`find ./Resources/lib -name *.so`; for s in $sos; do codesign -f -s "$id" $s; done
sos=`find ./Frameworks -name *.dylib`; for s in $sos; do codesign -f -s "$id" $s; done
sos=`find ./Frameworks -name *.so`; for s in $sos; do codesign -f -s "$id" $s; done
sos=`find ./Frameworks -name *.o`; for s in $sos; do codesign -f -s "$id" $s; done
sos=`find ./Frameworks -name *.a`; for s in $sos; do codesign -f -s "$id" $s; done
